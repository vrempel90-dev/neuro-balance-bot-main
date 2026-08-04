import type { Context, SessionFlavor } from "grammy";

export type Language = "ru" | "kk";
export type InputStep =
  | "consultation_details"
  | "document_details"
  | "lawyer_name"
  | "lawyer_contact"
  | "lawyer_issue";

export interface SessionData {
  language?: Language;
  screen: string;
  history: string[];
  awaiting?: InputStep;
  consultation?: { area: string; details?: string };
  document?: { type: string; contractType?: string; answer?: string };
  lawyerRequest?: { name?: string; contact?: string; issue?: string };
}

export type BotContext = Context & SessionFlavor<SessionData>;
export const initialSession = (): SessionData => ({
  screen: "language",
  history: [],
});
