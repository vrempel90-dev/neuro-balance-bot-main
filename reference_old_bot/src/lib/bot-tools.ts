import { getAvailableSlots, createAppointment, getLeadById, SlotConflictError, DoctorNotScheduledError } from '@/lib/crm-queries'
import { updateConversationMode, updateBotStatus, createMessage, createOutcome } from '@/lib/chat-queries'
import { getDoctorSpecializations, findDoctorsForDiagnosis, resolveDoctor } from '@/lib/doctor-queries'
import { getClinicInfoText, getClinicInfoTopics } from '@/lib/bot-clinic-info'
import { notifyBooking, notifyEscalation, notifyCompletedDialog, notifyAlreadyBookedEscalation } from '@/lib/telegram-notify'
import { sql } from '@/lib/db'
import { logBotAction, addBotNote } from '@/lib/bot-logger'
import { saveConversationToDataset } from '@/lib/training-queries'
import { evaluateLeadGuard } from '@/lib/bot-lead-guard'
import { evaluateContraindications, PROCEED_MARKER } from '@/lib/bot-contraindications'
import { formatExistingAppointmentMessage, type ExistingAppointmentRow } from '@/lib/bot-existing-appointment'
import { COMPLAINT_OK_MARKER, formatRecordComplaintMessage, formatComplaintGateMessage } from '@/lib/bot-complaint-gate'
import { formatBookingConfirmation, BOOKING_CONFIRMATION_MARKER } from '@/lib/bot-booking-confirmation'
import { sendWhatsAppMessage } from '@/lib/channel-sender'
import { getLatestInboundChannelId } from '@/lib/chat-queries'
import type { ToolDefinition, BotContext } from '@/types/bot'

// ─── TOOL DEFINITIONS (для AI) ─────────────────────────────────────────────

export const BOT_TOOLS: ToolDefinition[] = [
  {
    name: 'find_existing_appointment',
    description: 'Найти активные приёмы пациента по его WhatsApp-номеру (на сегодня и позже). ВСЕГДА вызывай ПЕРВЫМ в новом диалоге, ДО предложения слотов / сбора ФИО / чек-листа. Если ещё НЕ вызывал в этом диалоге — обязательно вызови, когда пациент упомянул дату/время до того, как ты предложил слот, либо написал «вы же записали», «у меня уже запись», «во сколько мне приходить», «адрес скиньте», «подтвердите запись», «перенести». verdict=found — подтверди запись, новый сценарий не запускай; verdict=none — продолжай обычный сценарий. Вызови один раз за диалог; результат закешируется.',
    parameters: {
      type: 'object',
      properties: {},
    },
  },
  {
    name: 'record_chief_complaint',
    description: 'Зафиксировать жалобу пациента — с чем он обратился. Вызови СРАЗУ после того, как пациент назвал, что его беспокоит, и ДО сбора возраста/времени, ДО чек-листа противопоказаний и ДО book_appointment. book_appointment программно заблокирован, пока профильная жалоба не зафиксирована этим инструментом.',
    parameters: {
      type: 'object',
      properties: {
        complaint: { type: 'string', description: 'Жалоба пациента его словами — с чем обратился (например: "боли в пояснице 2 недели", "немеет рука").' },
        is_in_profile: { type: 'boolean', description: 'true — заболевание профильное для клиники (неврология, позвоночник, суставы — см. секции профильных/непрофильных заболеваний); false — непрофильное (дальше mark_irrelevant, без записи).' },
      },
      required: ['complaint', 'is_in_profile'],
    },
  },
  {
    name: 'check_available_slots',
    description: 'Проверить свободные слоты для записи. Вызывай перед предложением времени. Для каждой даты вызывай ТОЛЬКО ОДИН РАЗ за диалог — после согласия пациента расписание повторно не перепроверяй. Слот с doctor_login="reserve" («Без закреплённого врача») — это свободное время без привязки к доктору, НЕ врач по фамилии «Резерв»: пациенту слово «Резерв» не говори, формулируй мягко («примет первый освободившийся врач»).',
    parameters: {
      type: 'object',
      properties: {
        date: { type: 'string', description: 'Дата в формате YYYY-MM-DD' },
        doctor_login: { type: 'string', description: 'Логин врача (необязательно, если не указан — все врачи)' },
      },
      required: ['date'],
    },
  },
  {
    name: 'book_appointment',
    description: 'Записать пациента на приём. Вызывай ТОЛЬКО после того, как verify_contraindications_check вернул proceed и пациент подтвердил конкретные дату и время. Телефон НЕ передавай — берётся из WhatsApp-номера автоматически. Для слота doctor_login="reserve" передавай "reserve" как есть; в подтверждении пациенту слово «Резерв» не используй («вас примет первый освободившийся врач»).',
    parameters: {
      type: 'object',
      properties: {
        patient_name: { type: 'string', description: 'ФИО пациента полностью' },
        doctor_login: { type: 'string', description: 'Логин врача' },
        doctor_name: { type: 'string', description: 'Имя врача для отображения' },
        date: { type: 'string', description: 'Дата в формате YYYY-MM-DD' },
        time_start: { type: 'string', description: 'Время начала в формате HH:MM' },
        notes: { type: 'string', description: 'Дата рождения или возраст пациента + жалоба, если упоминалась' },
      },
      required: ['patient_name', 'doctor_login', 'date', 'time_start'],
    },
  },
  {
    name: 'escalate_to_human',
    description: 'Передать диалог оператору-человеку. Вызывай когда не можешь помочь или пациент просит живого человека.',
    parameters: {
      type: 'object',
      properties: {
        reason: { type: 'string', description: 'Причина передачи' },
      },
      required: ['reason'],
    },
  },
  {
    name: 'get_doctor_info',
    description: 'Получить информацию о врачах клиники и их специализациях по диагнозу/жалобе. Используй ТОЛЬКО результат этого инструмента — НЕ генерируй имена врачей из общих знаний. Если инструмент возвращает «не найдено» / «ВНИМАНИЕ: ни один врач...» — это значит подходящего врача в клинике нет, нужно вызывать escalate_to_human, а НЕ выдумывать имя/биографию/опыт врача.',
    parameters: {
      type: 'object',
      properties: {
        diagnosis: { type: 'string', description: 'Диагноз или жалоба для поиска подходящего врача (необязательно)' },
      },
    },
  },
  {
    name: 'mark_irrelevant',
    description: 'Отметить заявку как непрофильную. Вызывай если заболевание пациента не входит в специализацию клиники.',
    parameters: {
      type: 'object',
      properties: {
        reason: { type: 'string', description: 'Почему непрофильный' },
      },
      required: ['reason'],
    },
  },
  {
    name: 'verify_contraindications_check',
    description: 'ОБЯЗАТЕЛЬНЫЙ шаг перед book_appointment. Зафиксируй ответы пациента на чек-лист противопоказаний (см. секцию «Чек-лист противопоказаний» в системном промпте) — инструмент сам вынесет вердикт: proceed / refuse / escalate. Если verdict=proceed — продолжай к book_appointment. Если refuse — вежливо откажи и предложи очный визит к врачу. Если escalate — вызови escalate_to_human с причиной из ответа.',
    parameters: {
      type: 'object',
      properties: {
        patient_age: { type: 'number', description: 'Возраст САМОГО пациента (того, кого записываем) полных лет — НЕ возраст члена семьи и НЕ срок болезни. Если известна только дата рождения — посчитай. Если возраст не удалось уточнить, передай 0 и добавь "patient_age" в uncertain_items.' },
        has_pacemaker_or_implant: { type: 'boolean', description: 'Кардиостимулятор / ИКД / инсулиновая помпа / кохлеарный имплант' },
        has_pregnancy: { type: 'boolean', description: 'Беременность на любом сроке' },
        has_active_cancer: { type: 'boolean', description: 'Активная онкология или подозрение, ещё не исключённое' },
        has_epilepsy: { type: 'boolean', description: 'Эпилепсия / судорожные приступы в анамнезе (любая форма, любая давность)' },
        has_thrombosis_or_bleeding: { type: 'boolean', description: 'Тромбофлебит / тромбоз / гемофилия / тромбоцитопения / острое кровотечение' },
        has_decompensated_endocrine: { type: 'boolean', description: 'Декомпенсированный сахарный диабет или тиреотоксикоз' },
        has_acute_infection_or_fever: { type: 'boolean', description: 'Температура >37.5 / ОРВИ / грипп / активный туберкулёз / гнойный процесс в зоне воздействия' },
        has_severe_cardio_or_respiratory: { type: 'boolean', description: 'Тяжёлая сердечная или дыхательная недостаточность, нестабильная стенокардия, инфаркт <6 мес' },
        has_severe_psychiatric: { type: 'boolean', description: 'Психоз или тяжёлое психическое расстройство в стадии обострения' },
        uncertain_items: {
          type: 'array',
          items: { type: 'string' },
          description: 'Имена полей, по которым пациент дал нечёткий ответ ("не знаю", "наверное", "не уверен"). Например ["has_thrombosis_or_bleeding", "patient_age"]. Если все ответы чёткие — пустой массив.',
        },
      },
      required: [
        'patient_age',
        'has_pacemaker_or_implant',
        'has_pregnancy',
        'has_active_cancer',
        'has_epilepsy',
        'has_thrombosis_or_bleeding',
        'has_decompensated_endocrine',
        'has_acute_infection_or_fever',
        'has_severe_cardio_or_respiratory',
        'has_severe_psychiatric',
        'uncertain_items',
      ],
    },
  },
  {
    name: 'get_clinic_info',
    description: 'Получить готовый шаблон ответа на типовой вопрос пациента (цена, график, МРТ, рассрочка, и т.п.). Вызывай ВСЕГДА когда пациент спрашивает про: цену первого приёма, цену курса лечения, нужен ли МРТ заранее, график работы, был у вас раньше и симптомы вернулись, как доехать, есть ли рассрочка/Каспи RED, субботняя предоплата, возврат предоплаты / жалоба (Отдел забот), просьба позвонить, методы лечения, возражение «дорого», возражение «подумаю», вопрос «поможет ли», пациент из другого города, пациент в неподвижном состоянии, эмпатия на болезнь пациента, голосовое сообщение. Инструмент возвращает дословный текст — отправь его пациенту КАК ЕСТЬ, не перефразируй и ничего не добавляй сверху. Это гарантирует единый стиль с операторами клиники и недопущение выдуманных фактов.',
    parameters: {
      type: 'object',
      properties: {
        topic: {
          type: 'string',
          enum: [
            'price_first_visit',
            'price_course',
            'mri_needed',
            'schedule',
            'returning_patient',
            'address',
            'installment',
            'kaspi_prepay_saturday',
            'refund_complaint',
            'phone_call_request',
            'methods',
            'objection_too_expensive',
            'objection_will_think',
            'helps_question',
            'other_city',
            'immobility_refuse',
            'empathy_illness',
            'voice_message_received',
          ],
          description: 'Какой блок ответа нужен.',
        },
      },
      required: ['topic'],
    },
  },
]

// ─── TOOL EXECUTION ─────────────────────────────────────────────────────────

export async function executeToolCall(
  toolName: string,
  args: Record<string, unknown>,
  ctx: BotContext,
  config: Record<string, string>,
): Promise<string> {
  switch (toolName) {
    case 'find_existing_appointment':
      return handleFindExistingAppointment(ctx)
    case 'record_chief_complaint':
      return handleRecordChiefComplaint(args, ctx)
    case 'check_available_slots':
      return handleCheckSlots(args, ctx)
    case 'book_appointment':
      return handleBookAppointment(args, ctx)
    case 'escalate_to_human':
      return handleEscalate(args, ctx)
    case 'get_doctor_info':
      return handleGetDoctorInfo(args, ctx)
    case 'mark_irrelevant':
      return handleMarkIrrelevant(args, ctx, config)
    case 'verify_contraindications_check':
      return handleVerifyContraindications(args, ctx)
    case 'get_clinic_info':
      return handleGetClinicInfo(args)
    default:
      return `Неизвестный инструмент: ${toolName}`
  }
}

function handleGetClinicInfo(args: Record<string, unknown>): string {
  const topic = String(args.topic ?? '')
  const text = getClinicInfoText(topic)
  if (!text) {
    return `Ошибка: неизвестный topic «${topic}». Доступные: ${getClinicInfoTopics().join(', ')}.`
  }
  return text
}

// ─── CONTRAINDICATIONS CHECK ────────────────────────────────────────────────
// Чистая логика вердикта вынесена в bot-contraindications.ts (для unit-тестов
// без зависимости от db.ts/DATABASE_URL).

async function handleVerifyContraindications(
  args: Record<string, unknown>,
  ctx: BotContext,
): Promise<string> {
  // Проверяем, есть ли у пациента активный приём (по conv.phone last-10 цифр).
  // Если есть — hard-stop конвертируется в escalate (не теряем уже-одобренного
  // оператором клиента; кейс +7 701 484-24-40, appt #1519).
  let appointmentLinked = false
  const phoneDigits = (ctx.phone || '').replace(/\D/g, '')
  if (phoneDigits.length >= 10) {
    const lastTen = phoneDigits.slice(-10)
    const rows = await sql`
      SELECT 1 FROM appointments
      WHERE date >= CURRENT_DATE
        AND status NOT IN ('ОТКАЗ_ПЕРЕНОС', 'НЕ_ПРИШЕЛ', 'НЕ_ПРИШЁЛ')
        AND (
          right(regexp_replace(COALESCE(phone, ''), '[^0-9]', '', 'g'), 10) = ${lastTen}
          OR right(regexp_replace(COALESCE(phone_raw, ''), '[^0-9]', '', 'g'), 10) = ${lastTen}
        )
      LIMIT 1
    `
    appointmentLinked = rows.length > 0
  }

  const decision = evaluateContraindications(args, { appointmentLinked })
  await logBotAction({
    conversationId: ctx.conversationId,
    leadId: ctx.leadId,
    actionType: 'tool_call',
    actionDetail: `Проверка противопоказаний: ${decision.verdict} (${decision.reason})${appointmentLinked ? ' [с активным приёмом]' : ''}`,
    toolName: 'verify_contraindications_check',
    toolArgs: args,
    toolResult: decision.messageForBot,
  })
  await addBotNote(
    ctx.leadId,
    decision.verdict === 'proceed'
      ? 'Чек-лист противопоказаний пройден.'
      : `Чек-лист противопоказаний: ${decision.verdict} (${decision.reason})`,
  )
  return decision.messageForBot
}

/**
 * Проверяет, был ли в этом диалоге успешный verify_contraindications_check
 * с вердиктом proceed. Программная страховка от попыток LLM записать,
 * минуя чек-лист (либо забыв вызвать инструмент, либо проигнорировав refuse/escalate).
 */
async function hasPassedContraindicationsCheck(conversationId: number): Promise<boolean> {
  const rows = await sql`
    SELECT 1
    FROM bot_action_logs
    WHERE conversation_id = ${conversationId}
      AND tool_name = 'verify_contraindications_check'
      AND tool_result LIKE ${'%' + PROCEED_MARKER + '%'}
    LIMIT 1
  `
  return rows.length > 0
}

/**
 * Был ли в этом диалоге verdict=refuse от verify_contraindications_check.
 * Если да — check_available_slots и book_appointment блокируются программно,
 * чтобы LLM не «открывала дверь» обратно после отказа (кейс +7 701 484-24-40,
 * где после refuse бот через 2 turn'а снова предлагал слот).
 */
async function hasRefusedContraindications(conversationId: number): Promise<boolean> {
  const rows = await sql`
    SELECT 1
    FROM bot_action_logs
    WHERE conversation_id = ${conversationId}
      AND tool_name = 'verify_contraindications_check'
      AND tool_result LIKE 'verdict=refuse%'
    LIMIT 1
  `
  return rows.length > 0
}

async function handleFindExistingAppointment(ctx: BotContext): Promise<string> {
  // Once-per-conversation guard: если за эту conv уже вызывали — вернём
  // последний tool_result из bot_action_logs. Иначе LLM в режиме greeting
  // может звать find_existing_appointment на каждый turn → 1 лишний SQL
  // на сообщение клиента. Кеш инвалидируется sentinel-записью из
  // handleBookAppointment после успешной брони.
  //
  // Time-window 24h защищает от случая, когда bot_action_logs очистится
  // ротацией между turn'ами (мало вероятно, но контракт явный).
  const cached = await sql`
    SELECT tool_result FROM bot_action_logs
    WHERE conversation_id = ${ctx.conversationId}
      AND tool_name = 'find_existing_appointment'
      AND created_at > NOW() - INTERVAL '24 hours'
    ORDER BY created_at DESC
    LIMIT 1
  ` as Array<{ tool_result: string }>
  if (cached.length > 0 && cached[0].tool_result) {
    return String(cached[0].tool_result)
  }

  // Поиск по conv.phone (last-10 цифр) ИЛИ по phone_raw — устойчиво к тому,
  // что в исторических данных phone был Wazzup chatId-ом (миграция 075
  // его сдвинула в phone_raw).
  const phoneDigits = (ctx.phone || '').replace(/\D/g, '')
  if (phoneDigits.length < 10) {
    const reason = 'verdict=none; reason=телефон не определён.'
    await logBotAction({
      conversationId: ctx.conversationId,
      leadId: ctx.leadId,
      actionType: 'tool_call',
      actionDetail: 'find_existing_appointment: телефон conv слишком короткий, пропуск',
      toolName: 'find_existing_appointment',
      toolResult: reason,
    })
    return reason
  }
  const lastTen = phoneDigits.slice(-10)
  const rows = await sql`
    SELECT id, to_char(date, 'YYYY-MM-DD') AS date,
           to_char(time_start, 'HH24:MI') AS time_start,
           doctor_login, doctor_name, patient_name, status
    FROM appointments
    WHERE date >= CURRENT_DATE
      AND status NOT IN ('ОТКАЗ_ПЕРЕНОС', 'НЕ_ПРИШЕЛ', 'НЕ_ПРИШЁЛ')
      AND (
        right(regexp_replace(COALESCE(phone, ''),     '[^0-9]', '', 'g'), 10) = ${lastTen}
        OR right(regexp_replace(COALESCE(phone_raw, ''), '[^0-9]', '', 'g'), 10) = ${lastTen}
      )
    ORDER BY date ASC, time_start ASC
    LIMIT 5
  ` as Array<{
    id: number; date: string; time_start: string;
    doctor_login: string; doctor_name: string;
    patient_name: string; status: string;
  }>

  const mapped: ExistingAppointmentRow[] = rows.map(r => ({
    id: r.id,
    date: r.date,
    timeStart: r.time_start,
    doctorLogin: r.doctor_login,
    doctorName: r.doctor_name,
    patientName: r.patient_name,
    status: r.status,
  }))
  const result = formatExistingAppointmentMessage(mapped)

  await logBotAction({
    conversationId: ctx.conversationId,
    leadId: ctx.leadId,
    actionType: 'tool_call',
    actionDetail: `find_existing_appointment: ${mapped.length} активных`,
    toolName: 'find_existing_appointment',
    toolResult: result.substring(0, 500),
  })
  if (mapped.length > 0) {
    await addBotNote(ctx.leadId, `Бот нашёл активные приёмы пациента: ${mapped.map(m => `#${m.id} ${m.date} ${m.timeStart}`).join(', ')}`)
  }
  return result
}

async function handleCheckSlots(args: Record<string, unknown>, ctx: BotContext): Promise<string> {
  const date = args.date as string
  const doctorLogin = args.doctor_login as string | undefined

  if (!date) return 'Ошибка: не указана дата'

  // Если в этом диалоге уже был refuse — слоты больше не показываем.
  // Иначе LLM на переспрос "так можно?" сразу зовёт check_available_slots
  // → book_appointment, обходя политику отказа.
  if (await hasRefusedContraindications(ctx.conversationId)) {
    await logBotAction({
      conversationId: ctx.conversationId,
      leadId: ctx.leadId,
      actionType: 'tool_call',
      actionDetail: 'check_available_slots отклонён: в диалоге уже был refuse',
      toolName: 'check_available_slots',
      toolArgs: args,
      toolResult: 'blocked: contraindications refused',
    })
    return 'Запись отклонена: в этом диалоге уже был отказ по противопоказаниям. Любую дальнейшую помощь оказывает оператор. Вызови escalate_to_human с reason="пациент настаивает после refuse — нужен оператор".'
  }

  const availability = await getAvailableSlots(date, doctorLogin, { patientPhone: ctx.phone })

  if (availability.length === 0) {
    return `На ${date} нет доступных врачей.`
  }

  const lines: string[] = [`Свободные слоты на ${date}:`]
  for (const doc of availability) {
    const freeSlots = doc.slots.filter(s => s.available).map(s => s.time)
    if (freeSlots.length > 0) {
      // Псевдо-врач 'reserve' выдаём LLM с нейтральным именем — иначе модель
      // дословно цитирует "Резерв" пациенту, что звучит грубо. Login оставляем
      // как есть (book_appointment ждёт 'reserve' и резолвит сам).
      const displayName = doc.doctorLogin === 'reserve'
        ? 'Без закреплённого врача (примет первый освободившийся)'
        : doc.doctorName
      lines.push(`${displayName} (${doc.doctorLogin}): ${freeSlots.join(', ')}`)
    }
  }

  if (lines.length === 1) {
    return `На ${date} все слоты заняты.`
  }

  await addBotNote(ctx.leadId, `Проверил свободные слоты на ${date}`)
  return lines.join('\n')
}

// ─── CHIEF COMPLAINT GATE ───────────────────────────────────────────────────
// Чистая логика — в bot-complaint-gate.ts. Здесь — DB-обёртка и гейт.

/**
 * Инструмент record_chief_complaint: фиксирует жалобу пациента в
 * bot_action_logs. Профильная непустая жалоба получает COMPLAINT_OK_MARKER
 * в tool_result — это разблокирует book_appointment (см. hasRecordedChiefComplaint).
 */
async function handleRecordChiefComplaint(
  args: Record<string, unknown>,
  ctx: BotContext,
): Promise<string> {
  const complaint = (args.complaint as string) || ''
  const isInProfile = args.is_in_profile === true
  const message = formatRecordComplaintMessage({ complaint, isInProfile })

  await logBotAction({
    conversationId: ctx.conversationId,
    leadId: ctx.leadId,
    actionType: 'tool_call',
    actionDetail: `Жалоба зафиксирована: "${complaint.slice(0, 100)}" (профильная: ${isInProfile ? 'да' : 'нет'})`,
    toolName: 'record_chief_complaint',
    toolArgs: args,
    toolResult: message,
  })

  if (complaint.trim()) {
    await addBotNote(
      ctx.leadId,
      isInProfile
        ? `Жалоба пациента: ${complaint.trim()}`
        : `Жалоба пациента (НЕПРОФИЛЬНАЯ): ${complaint.trim()}`,
    )
  }

  return message
}

/**
 * Была ли в этом диалоге зафиксирована профильная жалоба через
 * record_chief_complaint (COMPLAINT_OK_MARKER в tool_result). Программный
 * гейт для book_appointment — закрывает регрессию 2026-05-20, когда бот вёл
 * к записи, не спросив, что беспокоит пациента, и не проверив профильность.
 */
async function hasRecordedChiefComplaint(conversationId: number): Promise<boolean> {
  const rows = await sql`
    SELECT 1
    FROM bot_action_logs
    WHERE conversation_id = ${conversationId}
      AND tool_name = 'record_chief_complaint'
      AND tool_result LIKE ${'%' + COMPLAINT_OK_MARKER + '%'}
    LIMIT 1
  `
  return rows.length > 0
}

async function handleBookAppointment(args: Record<string, unknown>, ctx: BotContext): Promise<string> {
  const patientName = (args.patient_name as string) || ctx.patientName
  const phone = (args.phone as string) || ctx.phone
  const rawDoctorLogin = (args.doctor_login as string) || ''
  const rawDoctorName = (args.doctor_name as string) || rawDoctorLogin
  const date = args.date as string
  const timeStart = args.time_start as string
  const notes = (args.notes as string) || ''

  if (!rawDoctorLogin || !date || !timeStart) {
    return 'Ошибка: не указаны врач, дата или время'
  }

  // Если в этом диалоге уже был refuse — записывать нельзя ни в коем случае.
  // Эта проверка выше hasPassedContraindicationsCheck потому что refuse тоже
  // создаёт запись в bot_action_logs, и без явного отсечения LLM может уломать
  // пациента ответить «правильно» и пройти второй чек.
  if (await hasRefusedContraindications(ctx.conversationId)) {
    await logBotAction({
      conversationId: ctx.conversationId,
      leadId: ctx.leadId,
      actionType: 'tool_call',
      actionDetail: 'book_appointment отклонён: в диалоге уже был refuse',
      toolName: 'book_appointment',
      toolArgs: args,
      toolResult: 'blocked: contraindications refused',
    })
    return 'Запись отклонена: в этом диалоге уже был отказ по противопоказаниям. Вызови escalate_to_human с reason="пациент настаивает после refuse".'
  }

  // Hard guard: записывать можно только после record_chief_complaint с
  // профильной жалобой. Если жалоба не зафиксирована — возвращаем LLM
  // строку-инструкцию (не ошибку): tool-loop в bot-engine.ts скормит её
  // обратно, и бот дособерёт жалобу в диалоге (механизм фоллбэка).
  // Стоит ВЫШЕ чек-листа противопоказаний — жалоба идёт первой по сценарию.
  if (!(await hasRecordedChiefComplaint(ctx.conversationId))) {
    await logBotAction({
      conversationId: ctx.conversationId,
      leadId: ctx.leadId,
      actionType: 'tool_call',
      actionDetail: 'book_appointment отклонён: профильная жалоба не зафиксирована',
      toolName: 'book_appointment',
      toolArgs: args,
      toolResult: 'blocked: chief complaint missing',
    })
    return formatComplaintGateMessage()
  }

  // Hard guard: записывать можно только после verify_contraindications_check
  // с вердиктом proceed. Это страховка на случай, если LLM решит срезать угол
  // (пропустить чек-лист или проигнорировать refuse/escalate). Без этой
  // проверки промпт-инструкции остаются благим пожеланием, а здесь —
  // юридически и медицински важная отсечка.
  if (!(await hasPassedContraindicationsCheck(ctx.conversationId))) {
    await logBotAction({
      conversationId: ctx.conversationId,
      leadId: ctx.leadId,
      actionType: 'tool_call',
      actionDetail: 'book_appointment отклонён: чек-лист противопоказаний не пройден',
      toolName: 'book_appointment',
      toolArgs: args,
      toolResult: 'blocked: contraindications check missing',
    })
    return 'Запись отклонена системой: сначала вызови verify_contraindications_check (после того, как пациент ответит на чек-лист противопоказаний). Если уже вызывал и получил refuse — отказ + предложить очный визит. Если escalate — вызови escalate_to_human. Записывать без verdict=proceed нельзя.'
  }

  // Резолвим логин/имя, которые прислала LLM, в реальную пару (users.login, users.name).
  // LLM иногда галлюцинирует: присылает login="Madi" вместо настоящего "zhuma_md",
  // или сокращённое имя ("Мади Мухтарович" вместо "Жумабек Мади Мухтарович").
  // Старый путь просто INSERT-ил эти строки — в расписании появлялись "призрачные"
  // колонки. Теперь: если резолв нашёл врача — берём канонические login+name;
  // если нет — сразу уходим в Резерв, чтобы администратор разобрался.
  const resolved = await resolveDoctor({ login: rawDoctorLogin, name: rawDoctorName })
  const requestedDoctorLogin = resolved?.login ?? rawDoctorLogin
  const requestedDoctorName = resolved?.name ?? rawDoctorName
  const unresolvedReason: string | null = resolved
    ? null
    : `логин "${rawDoctorLogin}" / имя "${rawDoctorName}" не найдены среди активных врачей (галлюцинация LLM)`

  // Стратегия: подтверждённого пациента теряем — НЕЛЬЗЯ.
  // 1) Если резолвер нашёл врача — пробуем записать к нему (без повторной
  //    проверки расписания: createAppointment сам бросит DoctorNotScheduledError
  //    если врача нет в графике дня, мы поймаем и уйдём в Резерв).
  // 2) Если резолвер не нашёл врача (галлюцинация LLM по логину/имени) —
  //    сразу идём в Резерв с force=true, минуя первую попытку.
  // 3) Любая ошибка первой попытки → тот же fallback в Резерв.
  //    Администратор переназначит по пометке в notes и Telegram-уведомлении.
  let appointment: Awaited<ReturnType<typeof createAppointment>> | null = null
  let finalDoctorName = requestedDoctorName
  let fallbackReason: string | null = unresolvedReason

  if (!unresolvedReason) {
    try {
      appointment = await createAppointment({
        leadId: ctx.leadId,
        doctorLogin: requestedDoctorLogin,
        doctorName: requestedDoctorName,
        patientName,
        phone,
        date,
        timeStart,
        notes,
        createdBy: 'bot',
      })
    } catch (err) {
      if (err instanceof SlotConflictError) {
        fallbackReason = `слот ${timeStart} занят у ${requestedDoctorName}`
      } else if (err instanceof DoctorNotScheduledError) {
        // LLM предложила врача вне его графика (no_schedule, day_off, вне часов,
        // пересечение с обедом). Пациента не теряем — уходим в Резерв с понятной
        // пометкой для администратора, он переназначит на того, кто реально работает.
        fallbackReason = `${requestedDoctorName} не работает ${date} в ${timeStart} (${err.reason})`
      } else {
        fallbackReason = err instanceof Error ? err.message : 'неизвестная ошибка первичной записи'
      }
    }
  }

  if (appointment === null) {
    // Либо LLM прислала несуществующего врача (unresolvedReason установлен
    // до первой попытки), либо первая попытка упала (fallbackReason
    // установлен в catch). В обоих случаях — один и тот же путь в Резерв.
    try {
      const fallbackNotes = notes
        ? `${notes}\n[Fallback от бота: исходный выбор — ${rawDoctorName} (${rawDoctorLogin}). Причина: ${fallbackReason}. Нужно переназначить.]`
        : `[Fallback от бота: исходный выбор — ${rawDoctorName} (${rawDoctorLogin}). Причина: ${fallbackReason}. Нужно переназначить.]`
      appointment = await createAppointment({
        leadId: ctx.leadId,
        doctorLogin: 'reserve',
        doctorName: 'Резерв',
        patientName,
        phone,
        date,
        timeStart,
        notes: fallbackNotes,
        createdBy: 'bot',
        force: true,
      })
      finalDoctorName = 'Резерв'
    } catch (err2) {
      const msg = err2 instanceof Error ? err2.message : 'Неизвестная ошибка'
      await logBotAction({
        conversationId: ctx.conversationId,
        leadId: ctx.leadId,
        actionType: 'error',
        actionDetail: `Ошибка записи (и fallback к Резерву упал): ${msg}. Исходная причина: ${fallbackReason}`,
        toolName: 'book_appointment',
        toolArgs: args,
        errorMessage: msg,
      })
      return `Ошибка записи: ${msg}`
    }
  }

  // Инвалидируем кеш find_existing_appointment — пишем sentinel-запись с
  // актуальным состоянием, чтобы повторный вызов tool'а в этом же conv не
  // вернул устаревший verdict=none. Без этого Rule 0 в bot-prompt про
  // триггеры «вы же записали» бесполезен в том же conv.
  await sql`
    INSERT INTO bot_action_logs (conversation_id, lead_id, action_type, tool_name, tool_result, action_detail)
    VALUES (
      ${ctx.conversationId},
      ${ctx.leadId},
      'tool_call',
      'find_existing_appointment',
      ${'verdict=found; reason=только что записан в этом диалоге через book_appointment.'},
      'cache invalidation после book_appointment'
    )
  `

  await updateBotStatus(ctx.conversationId, 'completed')
  await createOutcome({
    conversationId: ctx.conversationId,
    outcome: 'booked',
    appointmentId: appointment.id,
  })
  saveConversationToDataset(ctx.conversationId, 'booked', '').catch(err =>
    console.error('[BOT] Dataset save failed:', err)
  )
  await notifyBooking({
    patientName,
    phone,
    doctorName: fallbackReason
      ? `Резерв (⚠️ исходно: ${requestedDoctorName}, ${fallbackReason} — переназначьте)`
      : finalDoctorName,
    date,
    timeStart,
    timeEnd: appointment.timeEnd,
    conversationId: ctx.conversationId,
  })
  sql`SELECT id FROM bot_training_dataset WHERE conversation_id = ${ctx.conversationId} ORDER BY created_at DESC LIMIT 1`
    .then(rows => {
      if (rows[0]?.id) {
        notifyCompletedDialog({
          conversationId: ctx.conversationId,
          outcome: 'booked',
          patientName,
          phone,
          datasetEntryId: rows[0].id as string,
        }).catch(() => {})
      }
    })
    .catch(() => {})

  const noteText = fallbackReason
    ? `⚠️ Записал к Резерву на ${date} ${timeStart}. Исходно: ${requestedDoctorName}. Причина fallback: ${fallbackReason}. Переназначьте.`
    : `Записал к ${finalDoctorName} на ${date} ${timeStart}`
  await addBotNote(ctx.leadId, noteText)

  await logBotAction({
    conversationId: ctx.conversationId,
    leadId: ctx.leadId,
    actionType: 'tool_call',
    actionDetail: fallbackReason
      ? `Запись в Резерв (fallback): ${patientName} ${date} ${timeStart}. Исходно: ${requestedDoctorName}. Причина: ${fallbackReason}`
      : `Запись: ${patientName} → ${finalDoctorName} ${date} ${timeStart}`,
    toolName: 'book_appointment',
    toolArgs: args,
    toolResult: `appointmentId: ${appointment.id}${fallbackReason ? ' [fallback=reserve]' : ''}`,
  })

  // Отправляем детерминированное подтверждение пациенту шаблоном. Гарантирует
  // правильный адрес (Кабанбай батыра 28, ...) — раньше LLM могла сгенерировать
  // выдуманный адрес («Кенесары 15»), или вообще ничего не вернуть (conv#6345:
  // «AI не вернул текстовый ответ», запись прошла без уведомления пациента).
  const confirmText = formatBookingConfirmation(
    patientName,
    appointment.date,
    appointment.timeStart,
    fallbackReason ? null : finalDoctorName,
  )
  try {
    const channelIdOverride = await getLatestInboundChannelId(ctx.conversationId)
    const sendResult = await sendWhatsAppMessage(
      ctx.externalId,
      confirmText,
      channelIdOverride ?? undefined,
    )
    if (sendResult.success) {
      await createMessage({
        conversationId: ctx.conversationId,
        direction: 'outbound',
        sender: 'bot',
        content: confirmText,
        metadata: { booking_confirmation: true, appointment_id: appointment.id },
      })
    } else {
      console.error('[BOT] booking confirmation send failed:', sendResult.error)
    }
  } catch (e) {
    console.error('[BOT] booking confirmation exception:', e)
  }

  // LLM получает тот же "успешный" ответ и в случае fallback — иначе
  // начнёт извиняться перед пациентом и терять заявку. Маркер
  // BOOKING_CONFIRMATION_SENT в начале строки — сигнал для bot-engine
  // НЕ отправлять finalText (подтверждение уже ушло выше). Маркер
  // санитизируется перед передачей в LLM, чтобы она не скопировала его.
  return `${BOOKING_CONFIRMATION_MARKER} | Запись создана! ID: ${appointment.id}, врач: ${requestedDoctorName}, дата: ${appointment.date}, время: ${appointment.timeStart}-${appointment.timeEnd}`
}

async function handleEscalate(args: Record<string, unknown>, ctx: BotContext): Promise<string> {
  const reason = (args.reason as string) || 'По запросу пациента'

  await updateConversationMode(ctx.conversationId, 'human')
  await updateBotStatus(ctx.conversationId, 'escalated')
  await createMessage({
    conversationId: ctx.conversationId,
    direction: 'outbound',
    sender: 'system',
    content: `Бот передал диалог оператору. Причина: ${reason}`,
    messageType: 'system',
  })
  await createOutcome({
    conversationId: ctx.conversationId,
    outcome: 'escalated',
  })
  saveConversationToDataset(ctx.conversationId, 'escalated', '').catch(err =>
    console.error('[BOT] Dataset save failed:', err)
  )
  // Reopened-closed detection: if the lead is in ЗАКРЫТО with a non-blacklisted
  // close_reason, the Telegram message gets a "🔁 Повторное обращение" prefix
  // so the operator immediately sees this is a returning patient.
  let closeReasonOnReopen: string | null = null
  try {
    if (ctx.leadId !== null) {
      const lead = await getLeadById(ctx.leadId)
      if (lead) {
        const guard = evaluateLeadGuard({
          status: lead.status,
          assignedTo: lead.assignedTo,
          closeReason: lead.closeReason ?? null,
        })
        if (guard.accept && guard.isReopenedClosed) {
          closeReasonOnReopen = lead.closeReason ?? null
        }
      }
    }
  } catch (e) {
    console.error('[bot] failed to detect reopened-closed:', e)
  }
  await notifyEscalation(ctx.conversationId, ctx.patientName, ctx.phone, reason, closeReasonOnReopen)

  // Если у пациента есть активная запись — отдельная нотификация в TG.
  // Запрос дублируется с handleFindExistingAppointment / handleVerifyContraindications,
  // но это lightweight SELECT 1 (right(phone,10) + LIMIT 1) — приемлемо.
  // TODO(perf): кеш existing-appt в ctx.botContext при следующем рефакторе BotContext.
  try {
    const phoneDigits = (ctx.phone || '').replace(/\D/g, '')
    if (phoneDigits.length >= 10) {
      const lastTen = phoneDigits.slice(-10)
      const rows = await sql`
        SELECT id, to_char(date, 'YYYY-MM-DD') AS d, to_char(time_start, 'HH24:MI') AS t,
               doctor_name
        FROM appointments
        WHERE date >= CURRENT_DATE
          AND status NOT IN ('ОТКАЗ_ПЕРЕНОС', 'НЕ_ПРИШЕЛ', 'НЕ_ПРИШЁЛ')
          AND (
            right(regexp_replace(COALESCE(phone, ''), '[^0-9]', '', 'g'), 10) = ${lastTen}
            OR right(regexp_replace(COALESCE(phone_raw, ''), '[^0-9]', '', 'g'), 10) = ${lastTen}
          )
        ORDER BY date ASC LIMIT 1
      ` as Array<{ id: number; d: string; t: string; doctor_name: string }>
      if (rows.length > 0) {
        const r = rows[0]
        await notifyAlreadyBookedEscalation({
          conversationId: ctx.conversationId,
          patientName: ctx.patientName ?? '',
          phone: ctx.phone ?? '',
          appointmentSummary: `appt #${r.id}, ${r.d} ${r.t}, ${r.doctor_name}`,
          reason,
        })
      }
    }
  } catch (err) {
    console.error('[BOT] notifyAlreadyBookedEscalation failed:', err)
  }
  // Send rating notification
  sql`SELECT id FROM bot_training_dataset WHERE conversation_id = ${ctx.conversationId} ORDER BY created_at DESC LIMIT 1`
    .then(rows => {
      if (rows[0]?.id) {
        notifyCompletedDialog({
          conversationId: ctx.conversationId,
          outcome: 'escalated',
          patientName: ctx.patientName,
          phone: ctx.phone,
          datasetEntryId: rows[0].id as string,
        }).catch(() => {})
      }
    })
    .catch(() => {})
  await addBotNote(ctx.leadId, `Передал оператору. Причина: ${reason}`)

  return `Диалог передан оператору. Причина: ${reason}`
}

async function handleGetDoctorInfo(args: Record<string, unknown>, ctx: BotContext): Promise<string> {
  const diagnosis = args.diagnosis as string | undefined

  const doctors = diagnosis
    ? await findDoctorsForDiagnosis(diagnosis)
    : await getDoctorSpecializations()

  if (doctors.length === 0) {
    // Anti-hallucination guard: пустой результат — это не «придумай врача».
    // В conv#6194 (Альмира) бот сгенерировал «Оспанову Асель Кайроллаевну»
    // с биографией («опыт 7 лет», «специализация: грыжи», «работает с
    // пациентами 32-56 лет») — выдумал ВСЁ. Пациент решил что у нас работает
    // эта Асель, при визите в клинику был бы скандал. Возвращаем директивный
    // текст, чтобы LLM знала: пустой результат = escalate, не генерация.
    return diagnosis
      ? `ВНИМАНИЕ: ни один врач клиники не специализируется на: ${diagnosis}. НЕ ВЫДУМЫВАЙ имя, ФИО, опыт или специализацию врача — это создаст ложные ожидания у пациента и приведёт к скандалу при визите. Скажи пациенту: «Подобрать профильного врача под Ваш запрос мне не удалось — давайте я передам диалог администратору, он подберёт специалиста.» и СРАЗУ вызови escalate_to_human с reason="нет профильного врача для запроса: ${diagnosis}".`
      : 'Список врачей пуст. Необходима настройка специализаций. НЕ выдумывай имена врачей — вызови escalate_to_human.'
  }

  const lines: string[] = diagnosis
    ? [`Врачи, которые лечат "${diagnosis}":`]
    : ['Врачи клиники:']

  for (const doc of doctors) {
    const parts: string[] = [`- ${doc.doctorName || doc.doctorLogin}`]
    if (doc.canTreat.length > 0) parts.push(`  Лечит: ${doc.canTreat.join(', ')}`)
    if (doc.preferredDiagnoses.length > 0) parts.push(`  Специализация: ${doc.preferredDiagnoses.join(', ')}`)
    if (doc.description) parts.push(`  ${doc.description}`)
    lines.push(parts.join('\n'))
  }

  return lines.join('\n')
}

async function handleMarkIrrelevant(
  args: Record<string, unknown>,
  ctx: BotContext,
  config: Record<string, string>,
): Promise<string> {
  const reason = (args.reason as string) || 'Непрофильный диагноз'

  await updateBotStatus(ctx.conversationId, 'rejected')
  await updateConversationMode(ctx.conversationId, 'closed')
  await createOutcome({
    conversationId: ctx.conversationId,
    outcome: 'rejected',
  })
  saveConversationToDataset(ctx.conversationId, 'rejected', '').catch(err =>
    console.error('[BOT] Dataset save failed:', err)
  )
  // Send rating notification
  sql`SELECT id FROM bot_training_dataset WHERE conversation_id = ${ctx.conversationId} ORDER BY created_at DESC LIMIT 1`
    .then(rows => {
      if (rows[0]?.id) {
        notifyCompletedDialog({
          conversationId: ctx.conversationId,
          outcome: 'rejected',
          patientName: ctx.patientName,
          phone: ctx.phone,
          datasetEntryId: rows[0].id as string,
        }).catch(() => {})
      }
    })
    .catch(() => {})
  await addBotNote(ctx.leadId, `Отклонил: ${reason}`)

  const farewell = config.farewell_message || 'Спасибо за обращение!'
  return `Заявка отмечена как непрофильная: ${reason}. Отправь пациенту прощальное сообщение.`
}
