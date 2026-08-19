import { Bot, InlineKeyboard, session } from "grammy";
import { CB, type MenuAction } from "./callbacks.js";
import { locales } from "./locales.js";
import { initialSession, type BotContext, type Language } from "./session.js";

const nav = (t: typeof locales.ru, back = true) => {
  const kb = new InlineKeyboard();
  if (back) kb.text(`‹ ${t.back}`, CB.back);
  return kb.text(`⌂ ${t.home}`, CB.home);
};
const languageKeyboard = () =>
  new InlineKeyboard()
    .text("Русский", `${CB.language}ru`)
    .text("Қазақша", `${CB.language}kk`);
const menuActions: MenuAction[] = [
  "consult",
  "prepare",
  "review",
  "cases",
  "prices",
  "lawyer",
  "language",
];

export function createBot(token = "test:token") {
  const bot = new Bot<BotContext>(token);
  bot.use(session({ initial: initialSession }));
  const t = (ctx: BotContext) => locales[ctx.session.language ?? "ru"];
  const show = async (
    ctx: BotContext,
    screen: string,
    text: string,
    keyboard: InlineKeyboard,
    push = true,
  ) => {
    if (push && ctx.session.screen !== screen)
      ctx.session.history.push(ctx.session.screen);
    ctx.session.screen = screen;
    ctx.session.awaiting = undefined;
    await ctx.reply(text, { reply_markup: keyboard });
  };
  const showLanguage = (ctx: BotContext, push = true) =>
    show(ctx, "language", t(ctx).chooseLanguage, languageKeyboard(), push);
  const showMenu = (ctx: BotContext, push = true) => {
    const m = t(ctx);
    const kb = new InlineKeyboard();
    m.menu.forEach((label, i) =>
      kb.text(label, `${CB.menu}${menuActions[i]}`).row(),
    );
    return show(ctx, "main", m.main, kb, push);
  };
  const list = (
    ctx: BotContext,
    screen: string,
    title: string,
    items: string[],
    prefix: string,
  ) => {
    const kb = new InlineKeyboard();
    items.forEach((x, i) => kb.text(x, `${prefix}${i}`).row());
    kb.text(`‹ ${t(ctx).back}`, CB.back).text(`⌂ ${t(ctx).home}`, CB.home);
    return show(ctx, screen, title, kb);
  };
  bot.command("start", async (ctx) => {
    ctx.session = initialSession();
    await ctx.reply(locales.ru.welcome, { reply_markup: languageKeyboard() });
  });
  bot.callbackQuery(new RegExp(`^${CB.language}(ru|kk)$`), async (ctx) => {
    await ctx.answerCallbackQuery();
    ctx.session.language = ctx.match[1] as Language;
    await showMenu(ctx);
  });
  bot.callbackQuery(new RegExp(`^${CB.menu}(.+)$`), async (ctx) => {
    await ctx.answerCallbackQuery();
    const action = ctx.match[1] as MenuAction;
    const m = t(ctx);
    if (action === "consult")
      return list(ctx, "consult", m.areasTitle, m.areas, CB.consultation);
    if (action === "prepare")
      return list(ctx, "prepare", m.docsTitle, m.docs, CB.document);
    if (action === "language") return showLanguage(ctx);
    if (action === "lawyer") {
      await show(ctx, "lawyer", m.lawyerName, nav(m));
      ctx.session.awaiting = "lawyer_name";
      return;
    }
    return show(
      ctx,
      action,
      m[
        action === "review" ? "review" : action === "cases" ? "cases" : "prices"
      ],
      nav(m),
    );
  });
  bot.callbackQuery(new RegExp(`^${CB.consultation}([0-9]+)$`), async (ctx) => {
    await ctx.answerCallbackQuery();
    const i = Number(ctx.match[1]);
    ctx.session.consultation = {
      area: t(ctx).areas[i] ?? t(ctx).areas.at(-1)!,
    };
    await show(ctx, "consult_input", t(ctx).describe, nav(t(ctx)));
    ctx.session.awaiting = "consultation_details";
  });
  bot.callbackQuery(new RegExp(`^${CB.document}([0-9]+)$`), async (ctx) => {
    await ctx.answerCallbackQuery();
    const i = Number(ctx.match[1]);
    const type = t(ctx).docs[i] ?? t(ctx).docs.at(-1)!;
    ctx.session.document = { type };
    if (i === 4)
      return list(
        ctx,
        "contracts",
        t(ctx).contractsTitle,
        t(ctx).contracts,
        CB.contract,
      );
    await show(ctx, "doc_input", t(ctx).docQuestion, nav(t(ctx)));
    ctx.session.awaiting = "document_details";
  });
  bot.callbackQuery(new RegExp(`^${CB.contract}([0-9]+)$`), async (ctx) => {
    await ctx.answerCallbackQuery();
    ctx.session.document = {
      type: t(ctx).docs[4]!,
      contractType:
        t(ctx).contracts[Number(ctx.match[1])] ?? t(ctx).contracts.at(-1)!,
    };
    await show(ctx, "doc_input", t(ctx).docQuestion, nav(t(ctx)));
    ctx.session.awaiting = "document_details";
  });
  bot.callbackQuery(CB.home, async (ctx) => {
    await ctx.answerCallbackQuery();
    ctx.session.history = [];
    await showMenu(ctx, false);
  });
  bot.callbackQuery(CB.back, async (ctx) => {
    await ctx.answerCallbackQuery();
    const previous = ctx.session.history.pop() ?? "main";
    if (previous === "language") return showLanguage(ctx, false);
    if (previous === "consult")
      return list(
        ctx,
        "consult",
        t(ctx).areasTitle,
        t(ctx).areas,
        CB.consultation,
      );
    if (previous === "prepare")
      return list(ctx, "prepare", t(ctx).docsTitle, t(ctx).docs, CB.document);
    if (previous === "contracts")
      return list(
        ctx,
        "contracts",
        t(ctx).contractsTitle,
        t(ctx).contracts,
        CB.contract,
      );
    return showMenu(ctx, false);
  });
  bot.on("message:text", async (ctx) => {
    const value = ctx.message.text.trim();
    const step = ctx.session.awaiting;
    const m = t(ctx);
    if (!step) return ctx.reply(m.textHint, { reply_markup: nav(m) });
    if (step === "consultation_details") {
      ctx.session.consultation!.details = value;
      ctx.session.awaiting = undefined;
      return ctx.reply(m.saved, { reply_markup: nav(m) });
    }
    if (step === "document_details") {
      ctx.session.document!.answer = value;
      ctx.session.awaiting = undefined;
      return ctx.reply(m.docSaved, { reply_markup: nav(m) });
    }
    ctx.session.lawyerRequest ??= {};
    if (step === "lawyer_name") {
      ctx.session.lawyerRequest.name = value;
      ctx.session.awaiting = "lawyer_contact";
      return ctx.reply(m.lawyerContact, { reply_markup: nav(m) });
    }
    if (step === "lawyer_contact") {
      ctx.session.lawyerRequest.contact = value;
      ctx.session.awaiting = "lawyer_issue";
      return ctx.reply(m.lawyerIssue, { reply_markup: nav(m) });
    }
    ctx.session.lawyerRequest.issue = value;
    ctx.session.awaiting = undefined;
    return ctx.reply(m.lawyerDone, { reply_markup: nav(m) });
  });
  return bot;
}
