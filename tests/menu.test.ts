import { describe, expect, it, vi } from "vitest";
import { createBot } from "../src/bot.js";

const update = (id: number, data?: string, text?: string) => ({
  update_id: id,
  ...(data
    ? {
        callback_query: {
          id: `${id}`,
          from: { id: 1, is_bot: false, first_name: "Test" },
          chat_instance: "x",
          data,
          message: {
            message_id: id,
            date: 1,
            chat: { id: 1, type: "private" as const },
            text: "menu",
          },
        },
      }
    : {
        message: {
          message_id: id,
          date: 1,
          chat: { id: 1, type: "private" as const },
          from: { id: 1, is_bot: false, first_name: "Test" },
          text: text!,
          ...(text === "/start"
            ? {
                entities: [
                  { offset: 0, length: 6, type: "bot_command" as const },
                ],
              }
            : {}),
        },
      }),
});
const setup = () => {
  const bot = createBot();
  const calls: any[] = [];
  vi.spyOn(bot.api, "callApi").mockImplementation(async (method, payload) => {
    calls.push([method, payload]);
    return true as never;
  });
  return { bot, calls };
};

describe("KORGAN menu flows", () => {
  it("shows language choice and Russian main menu", async () => {
    const { bot, calls } = setup();
    await bot.handleUpdate(update(1, undefined, "/start"));
    await bot.handleUpdate(update(2, "language:ru"));
    expect(
      calls.some(([, p]) => String(p.text).includes("Получить консультацию")),
    ).toBe(true);
  });
  it("collects a consultation without giving advice", async () => {
    const { bot, calls } = setup();
    await bot.handleUpdate(update(1, undefined, "/start"));
    await bot.handleUpdate(update(2, "language:ru"));
    await bot.handleUpdate(update(3, "menu:consult"));
    await bot.handleUpdate(update(4, "consultation:0"));
    await bot.handleUpdate(update(5, undefined, "Описание спора"));
    expect(
      calls.some(([, p]) => String(p.text).includes("текущей сессии")),
    ).toBe(true);
  });
  it("opens contract types and supports home navigation", async () => {
    const { bot, calls } = setup();
    await bot.handleUpdate(update(1, undefined, "/start"));
    await bot.handleUpdate(update(2, "language:kk"));
    await bot.handleUpdate(update(3, "menu:prepare"));
    await bot.handleUpdate(update(4, "document:4"));
    expect(
      calls.some(([, p]) => String(p.text).includes("Қызмет көрсету шарты")),
    ).toBe(true);
    await bot.handleUpdate(update(5, "nav:home"));
    expect(calls.at(-1)?.[1].text).toBe("Басты мәзір");
  });
});
