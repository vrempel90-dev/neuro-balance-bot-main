// Детектор возраста пациента из текста сообщения. Pure-модуль (без БД/IO),
// чтобы можно было тестировать unit-тестами без DATABASE_URL.
//
// Используется в bot-engine как программный age-guard перед AI loop:
// если возраст вне допустимого диапазона (16–74), бот сразу отказывает
// и эскалирует, не зависимо от того, вспомнит ли LLM позвать
// verify_contraindications_check. Истоки: кейс conv 5378, +77783024817
// (75 лет, 14.05.2026) — бот развернул чек-лист текстом, не вызвал
// инструмент, пациентка сменила тему — отказа никто не дал, оператор
// разруливал утром.
//
// Дизайн консервативный: лучше пропустить (LLM подхватит), чем дать
// ложный hard-stop на невинной фразе вроде «дом 75» или «процедура
// длится 75 минут».

export interface DetectedAge {
  age: number
  source: 'dob' | 'explicit'
  /** Подстрока, которую сматчили — для логов / отладки. */
  matched: string
}

/**
 * Возвращает возраст пациента в полных годах, если в тексте найдена
 * либо дата рождения (DD.MM.YYYY с вариантами), либо явное упоминание
 * возраста («мне 75 лет», «75 годик»).
 *
 * Возвращает null, если ничего достоверно не сматчилось или результат
 * вне разумного диапазона (1..130).
 *
 * @param text  Текст сообщения от пациента (после Whisper-транскрипции).
 * @param today Текущая дата (для подсчёта возраста от ДР). Параметр
 *              явный, чтобы тесты могли фиксировать «сегодня».
 */
export function detectAge(text: string, today: Date): DetectedAge | null {
  if (!text) return null
  const dob = matchDateOfBirth(text, today)
  if (dob) return dob
  const explicit = matchExplicitAge(text)
  if (explicit) return explicit
  return null
}

// Полный год = разница в годах с поправкой на «не наступил день рождения».
function calcAgeFromDob(dobYear: number, dobMonth: number, dobDay: number, today: Date): number {
  let age = today.getFullYear() - dobYear
  const beforeBirthday =
    today.getMonth() + 1 < dobMonth ||
    (today.getMonth() + 1 === dobMonth && today.getDate() < dobDay)
  if (beforeBirthday) age -= 1
  return age
}

// Принимаем разделители: точка, слэш, дефис, пробел.
// Год: 4 цифры (1900-2099) или 2 цифры (00-99) — но 2-значный год
// не поддерживаем, чтобы не угадывать столетие (45 — это 1945 или 2045?).
const DOB_RE = /\b(\d{1,2})[.\-\/\s](\d{1,2})[.\-\/\s](\d{4})(?:\s*г\.?)?\b/

function matchDateOfBirth(text: string, today: Date): DetectedAge | null {
  const m = DOB_RE.exec(text)
  if (!m) return null
  const day = parseInt(m[1], 10)
  const month = parseInt(m[2], 10)
  const year = parseInt(m[3], 10)
  if (day < 1 || day > 31) return null
  if (month < 1 || month > 12) return null
  if (year < 1900 || year > today.getFullYear()) return null
  // Будущая дата (например, «дату приёма») — не ДР.
  const candidate = new Date(year, month - 1, day)
  if (candidate.getTime() > today.getTime()) return null
  const age = calcAgeFromDob(year, month, day, today)
  if (age < 1 || age > 130) return null
  return { age, source: 'dob', matched: m[0] }
}

// Явный возраст засчитываем ТОЛЬКО при высокой уверенности — два прохода:
//   Проход 2: сильный возрастной маркер (мне/маган/маған/возраст) + число.
//   Проход 3: всё сообщение целиком — это возраст.
// Безусловный матч «<число> + лет/года где угодно» УДАЛЁН: именно он давал
// ложный hard-stop на анамнезе («грыжа 3 года назад», «болею 3 года»).
// Слабый маркер «я» — только в проходе 3 (целым сообщением), в проходе 2
// его нет: «я 3 года болею» неоднозначно.
// Проход 2 — сильный маркер перед числом, роуминг по тексту. Год-единица
// после числа не обязательна (маркер сам по себе достаточен: «мне 45»).
// Регекс — ЛИТЕРАЛ (не new RegExp от строки): экранирование однозначно,
// без ловушки с двойными слешами. [^а-яА-Яa-zA-Z\d] = граница токена.
const STRONG_MARKER_AGE_RE =
  /(?:^|[^а-яА-Яa-zA-Z\d])(?:мне|маған|маган|возраст)(?:[\s:]+(?:сейчас|уже|будет))?[\s:]+(\d{1,3})(?=$|[^а-яА-Яa-zA-Z\d])/i

// Проход 3 — всё сообщение (после trim) является возрастом: опциональное
// ведущее «я»/«мне», число, опциональная год-единица, опциональные знаки.
const WHOLE_MESSAGE_AGE_RE =
  /^(я|мне)?\s*(\d{1,3})\s*(?:лет|года?|годик[аи]?в?|жас)?[.!?]*$/i

const MIN_PLAUSIBLE_AGE = 1
const MAX_PLAUSIBLE_AGE = 130
// Нижний порог доверия для голого «<число>» без местоимения (проход 3).
// Голое число < 16 целым сообщением в чате записи — это почти всегда срок
// болезни («3 года» в ответ на «как давно болит?»), а не возраст. Hard-stop
// по нижней границе по такому сообщению guard сознательно НЕ ставит —
// отдаёт LLM (Шаг 3 сценария). Безопасный false negative.
const BARE_WHOLE_MESSAGE_MIN_AGE = 16

function inPlausibleRange(age: number): boolean {
  return age >= MIN_PLAUSIBLE_AGE && age <= MAX_PLAUSIBLE_AGE
}

function matchExplicitAge(text: string): DetectedAge | null {
  // Проход 2: сильный маркер + число.
  const m = STRONG_MARKER_AGE_RE.exec(text)
  if (m) {
    const age = parseInt(m[1], 10)
    if (inPlausibleRange(age)) {
      return { age, source: 'explicit', matched: m[0].trim() }
    }
  }
  // Проход 3: всё сообщение — это возраст.
  const w = WHOLE_MESSAGE_AGE_RE.exec(text.trim())
  if (w) {
    const hasPronoun = Boolean(w[1])
    const age = parseInt(w[2], 10)
    if (inPlausibleRange(age) && (hasPronoun || age >= BARE_WHOLE_MESSAGE_MIN_AGE)) {
      return { age, source: 'explicit', matched: w[0].trim() }
    }
  }
  return null
}

/**
 * Граница «hard-stop по возрасту»: возраст, при котором бот обязан отказать
 * в записи. Политика клиники: принимаем 16–74 включительно (75 уже нет).
 * Единственный источник истины — эта функция; bot-contraindications.ts её
 * импортирует.
 */
export function isAgeHardStop(age: number): boolean {
  return age < 16 || age >= 75
}
