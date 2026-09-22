// Чистая логика валидации казахстанского мобильного телефона.
// KZ-мобильный — это 11 цифр, начинающихся с "77" или "87" (8 → 7 нормализация).
// Любой Wazzup-внутренний chatId (длина 12-14, не KZ-формат) считается невалидным.
//
// Создано для борьбы с кейсом, когда Wazzup-webhook сохранял chatId
// "46561790787743" как телефон лида и приёма — после этого findLeadByPhone
// промахивался по реальному номеру пациента, плодились лиды-дубликаты.

function digitsOnly(value: unknown): string {
  if (typeof value !== 'string') return ''
  return value.replace(/\D/g, '')
}

/**
 * Является ли строка валидным KZ-мобильным.
 * Принимаем три формата:
 *   - 11 цифр, начало "77" (международный +7 770...)
 *   - 11 цифр, начало "87" (казахстанский 8-770...)
 *   - 10 цифр, начало "7"  (национальный без кода страны, формат Wazzup contact.phone)
 * Wazzup-внутренний chatId (12-14 цифр) и иностранные номера (79..., 33...,
 * 998...) отвергаются — флаг phone_needs_review остаётся валидным сигналом.
 */
export function isValidKzMobile(phone: unknown): boolean {
  const d = digitsOnly(phone)
  if (d.length === 11) {
    const normalized = d.startsWith('8') ? '7' + d.slice(1) : d
    return normalized.startsWith('77')
  }
  // 10-значный национальный: "7XX...". Wazzup присылает именно в этом формате
  // для contact.phone. Раньше отбраковывался — отсюда 870 проблемных лидов.
  if (d.length === 10) return d.startsWith('7')
  return false
}

/**
 * Канонизирует валидный KZ-мобильный в формат "77XXXXXXXXX" (11 цифр).
 * Невалидный → пустая строка. НЕ кидает.
 */
export function sanitizeKzPhone(phone: unknown): string {
  if (!isValidKzMobile(phone)) return ''
  const d = digitsOnly(phone)
  if (d.length === 10) return '7' + d            // 10 → добавляем код страны
  if (d.startsWith('8')) return '7' + d.slice(1) // 87... → 77...
  return d                                       // 77... уже в каноне
}
