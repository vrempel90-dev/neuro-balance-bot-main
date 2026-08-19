export const CB = {
  language: "language:",
  menu: "menu:",
  consultation: "consultation:",
  document: "document:",
  contract: "contract:",
  back: "nav:back",
  home: "nav:home",
} as const;

export type MenuAction =
  | "consult"
  | "prepare"
  | "review"
  | "cases"
  | "prices"
  | "lawyer"
  | "language";
