// Чистая логика вердикта по чек-листу противопоказаний.
// БД и побочные эффекты живут в bot-tools.ts; тут только правила.
//
// Утверждено пользователем 2026-05-07. См. memory:
// project_neuro_booking_contraindications.md

import { isAgeHardStop } from '@/lib/bot-age-detector'

/**
 * Маркер в bot_action_logs.tool_result, по которому handleBookAppointment
 * проверяет, прошёл ли пациент чек-лист с вердиктом proceed.
 * Меняешь формат — обнови запрос в hasPassedContraindicationsCheck().
 */
export const PROCEED_MARKER = 'verdict=proceed'

export const HARD_STOP_FIELDS = [
  'has_pacemaker_or_implant',
  'has_pregnancy',
  'has_active_cancer',
  'has_epilepsy',
] as const

export const SOFT_STOP_FIELDS = [
  'has_thrombosis_or_bleeding',
  'has_decompensated_endocrine',
  'has_acute_infection_or_fever',
  'has_severe_cardio_or_respiratory',
  'has_severe_psychiatric',
] as const

export const FIELD_LABELS: Record<string, string> = {
  patient_age: 'возраст',
  has_pacemaker_or_implant: 'кардиостимулятор/имплант',
  has_pregnancy: 'беременность',
  has_active_cancer: 'онкология',
  has_epilepsy: 'эпилепсия',
  has_thrombosis_or_bleeding: 'тромбоз/кровотечение',
  has_decompensated_endocrine: 'декомпенсированный СД/тиреотоксикоз',
  has_acute_infection_or_fever: 'острая инфекция/лихорадка',
  has_severe_cardio_or_respiratory: 'тяжёлая ССН/ДН',
  has_severe_psychiatric: 'психическое расстройство в обострении',
}

export type ContraindicationVerdict = 'proceed' | 'refuse' | 'escalate'

export interface ContraindicationDecision {
  verdict: ContraindicationVerdict
  reason: string
  messageForBot: string
}

export interface EvaluationContext {
  /** У пациента уже есть подтверждённая будущая запись на приём. Если true и hard-stop сработал — конвертируется в escalate (не теряем уже-одобренного оператором клиента). */
  appointmentLinked?: boolean
}

/**
 * Правила (утв. пользователем 2026-05-07, граница уточнена 2026-05-15):
 * - Возраст вне [16, 74] (если указан) → refuse. Реальная политика клиники
 *   по сообщению оператора 15.05.2026 (кейс 7783024817, 75 лет) — «принимаем
 *   до 75 лет», то есть 75 уже не принимаем.
 * - Любой hard-stop (кардио-имплант, беременность, онко, эпилепсия) → refuse.
 * - Любой soft-stop или uncertain (включая age=0) → escalate.
 * - Иначе → proceed.
 *
 * Если ctx.appointmentLinked=true (пациент уже записан оператором), любой
 * refuse конвертируется в escalate — оператор разберётся вручную.
 */
export function evaluateContraindications(
  args: Record<string, unknown>,
  ctx: EvaluationContext = {},
): ContraindicationDecision {
  const age = typeof args.patient_age === 'number' ? args.patient_age : 0
  const uncertainItems = Array.isArray(args.uncertain_items)
    ? (args.uncertain_items as unknown[]).map(String)
    : []

  const hardHits: string[] = []
  for (const field of HARD_STOP_FIELDS) {
    if (args[field] === true) hardHits.push(FIELD_LABELS[field])
  }
  // age > 0 ОБЯЗАТЕЛЬНО: isAgeHardStop(0) === true, а age=0 = «возраст
  // неизвестен» → uncertain/escalate ниже, не hard-stop.
  const ageBlocked = age > 0 && isAgeHardStop(age)
  if (ageBlocked) {
    hardHits.push(`возраст ${age} лет (допустимо 16–74)`)
  }

  const softHits: string[] = []
  for (const field of SOFT_STOP_FIELDS) {
    if (args[field] === true) softHits.push(FIELD_LABELS[field])
  }

  const allUncertain = age === 0 && !uncertainItems.includes('patient_age')
    ? [...uncertainItems, 'patient_age']
    : uncertainItems
  const uncertainLabels = allUncertain
    .map(item => FIELD_LABELS[item] ?? item)
    .filter(Boolean)

  // Соглашение: меняем поведение ТОЛЬКО для hard-stop ветки. Soft-stop и
  // proceed-ветки остаются прежними (escalate/proceed) — там и так либо
  // оператор разберётся, либо ничего страшного не происходит.
  if (hardHits.length > 0) {
    const reason = `hard-stop: ${hardHits.join(', ')}`
    if (ctx.appointmentLinked) {
      // Пациент УЖЕ был одобрен оператором и записан. Текстовый отказ от
      // бота тут разрушает доверие и теряет клиента — оператор разберётся.
      return {
        verdict: 'escalate',
        reason: `${reason}; запись уже подтверждена оператором`,
        messageForBot: `verdict=escalate; reason=${reason}, запись уже подтверждена оператором. Запись делать НЕЛЬЗЯ, отказ давать тоже НЕЛЬЗЯ. Скажи пациенту: "Мне нужно уточнить детали приёма у координатора — она напишет вам в ближайшее время." Затем сразу вызови escalate_to_human с reason="hard-stop противопоказание у уже записанного пациента: ${reason}".`,
      }
    }
    return {
      verdict: 'refuse',
      reason,
      messageForBot: `verdict=refuse; reason=${reason}. Метод противопоказан. Вежливо откажи в записи: объясни, что высокоинтенсивная физиотерапия в данном случае противопоказана, и предложи прийти на очную консультацию к врачу для подбора альтернативного метода. После отказа НЕ вызывай book_appointment. Если пациент настаивает — вызови escalate_to_human.`,
    }
  }
  if (softHits.length > 0 || uncertainLabels.length > 0) {
    const parts: string[] = []
    if (softHits.length > 0) parts.push(`soft-stop: ${softHits.join(', ')}`)
    if (uncertainLabels.length > 0) parts.push(`не уточнено: ${uncertainLabels.join(', ')}`)
    const reason = parts.join('; ')
    return {
      verdict: 'escalate',
      reason,
      messageForBot: `verdict=escalate; reason=${reason}. Запись делать НЕЛЬЗЯ. Скажи пациенту: "Эта ситуация требует уточнения у наших врачей — я передам ваш вопрос координатору, и мы вернёмся с ответом завтра." Затем сразу вызови escalate_to_human с reason="Противопоказания: ${reason}". НЕ вызывай book_appointment.`,
    }
  }
  return {
    verdict: 'proceed',
    reason: 'no contraindications reported',
    messageForBot: `${PROCEED_MARKER}; reason=no contraindications reported. Противопоказаний нет — можно продолжать к book_appointment.`,
  }
}
