import { NextResponse } from 'next/server'
import { createAppointment } from '@/lib/crm-queries'
import { updateBotStatus, createOutcome } from '@/lib/chat-queries'
import { notifyBooking } from '@/lib/telegram-notify'

// POST /api/bot/book — бот записывает пациента на приём

export async function POST(req: Request) {
  try {
    const secret = req.headers.get('x-bot-secret')
    if (secret !== process.env.BOT_API_SECRET) {
      return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })
    }

    const body = await req.json()
    const { patientName, phone, doctorLogin, doctorName, date, timeStart, notes, conversationId, leadId } = body

    if (!patientName || !phone || !doctorLogin || !date || !timeStart) {
      return NextResponse.json({
        error: 'patientName, phone, doctorLogin, date, timeStart required',
      }, { status: 400 })
    }

    // Создаём запись через существующую функцию
    const appointment = await createAppointment({
      leadId: leadId ?? null,
      doctorLogin,
      doctorName: doctorName ?? doctorLogin,
      patientName,
      phone,
      date,
      timeStart,
      notes: notes ?? '',
      createdBy: 'bot',
    })

    // Обновляем статус диалога
    if (conversationId) {
      await updateBotStatus(conversationId, 'completed')
      await createOutcome({
        conversationId,
        outcome: 'booked',
        appointmentId: appointment.id,
      })
    }

    // Уведомляем в Telegram
    await notifyBooking({
      patientName,
      phone,
      doctorName: doctorName ?? doctorLogin,
      date,
      timeStart,
      timeEnd: appointment.timeEnd,
      conversationId: conversationId ?? undefined,
    })

    return NextResponse.json({
      appointmentId: appointment.id,
      doctorName: appointment.doctorName,
      date: appointment.date,
      timeStart: appointment.timeStart,
      timeEnd: appointment.timeEnd,
    }, { status: 201 })
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : 'Internal error'
    console.error('POST /api/bot/book:', error)
    return NextResponse.json({ error: message }, { status: 500 })
  }
}
