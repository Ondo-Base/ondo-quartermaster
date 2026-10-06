// Inline stroke icons on a 24px grid, exactly as drawn in design/artboards:
// fill none, currentColor, 1.6 stroke (2 for check and cross), round caps.

import type { CSSProperties, ReactNode } from "react";

const P: Record<string, ReactNode> = {
  check: <path d="M5 13l4 4 10-10" />,
  close: <><path d="M6 6l12 12" /><path d="M18 6L6 18" /></>,
  "arrow-right": <><path d="M5 12h14" /><path d="M13 6l6 6-6 6" /></>,
  "arrow-left": <><path d="M19 12H5" /><path d="M11 6l-6 6 6 6" /></>,
  "arrow-up": <><path d="M12 20V5" /><path d="M6 11l6-6 6 6" /></>,
  folder: <path d="M3.5 7.5A1.5 1.5 0 0 1 5 6h4l2 2.5h8A1.5 1.5 0 0 1 20.5 10v8A1.5 1.5 0 0 1 19 19.5H5A1.5 1.5 0 0 1 3.5 18z" />,
  lock: <><rect x="4.5" y="10.5" width="15" height="9.5" rx="1.5" /><path d="M8 10.5V7.5a4 4 0 0 1 8 0v3" /></>,
  monitor: <><rect x="3" y="4" width="18" height="13" rx="1.5" /><path d="M9 20h6" /><path d="M12 17v3" /></>,
  workflow: <><path d="M4 7h16" /><path d="M4 12h16" /><path d="M4 17h10" /></>,
  file: <><path d="M14 3H7a1.5 1.5 0 0 0-1.5 1.5v15A1.5 1.5 0 0 0 7 21h10a1.5 1.5 0 0 0 1.5-1.5V7.5z" /><path d="M14 3v4.5h4.5" /></>,
  mail: <><rect x="3" y="5.5" width="18" height="13" rx="1.5" /><path d="M3.5 7l8.5 6 8.5-6" /></>,
  card: <><rect x="2.5" y="6" width="19" height="12" rx="1.5" /><path d="M8 14h8" /></>,
  keyboard: <><rect x="2.5" y="6" width="19" height="12" rx="1.5" /><path d="M6.5 10h1" /><path d="M10.5 10h1" /><path d="M14.5 10h1" /><path d="M8 14h8" /></>,
  chat: <path d="M20 15.5A2.5 2.5 0 0 1 17.5 18H9l-4 3v-3.5A2.5 2.5 0 0 1 4 15.5v-8A2.5 2.5 0 0 1 6.5 5h11A2.5 2.5 0 0 1 20 7.5z" />,
  search: <><circle cx="11" cy="11" r="6.5" /><path d="M16 16l4 4" /></>,
  document: <><rect x="4" y="4" width="16" height="16" rx="1.5" /><path d="M8 9h8" /><path d="M8 13h8" /><path d="M8 17h4" /></>,
  "panel-close": <><path d="M4 5h16" /><path d="M4 12h9" /><path d="M4 19h16" /><path d="M20 9l-3 3 3 3" /></>,
  "sign-out": <><path d="M14 20H6.5A1.5 1.5 0 0 1 5 18.5v-13A1.5 1.5 0 0 1 6.5 4H14" /><path d="M17 8l4 4-4 4" /><path d="M21 12h-10" /></>,
  plus: <><path d="M12 5v14" /><path d="M5 12h14" /></>,
  upload: <><path d="M12 4v10" /><path d="M7 9l5-5 5 5" /><path d="M5 20h14" /></>,
  globe: <><circle cx="12" cy="12" r="9" /><path d="M3.5 9h17" /><path d="M3.5 15h17" /><path d="M12 3c2.5 2.5 2.5 15 0 18" /><path d="M12 3c-2.5 2.5-2.5 15 0 18" /></>,
  info: <><circle cx="12" cy="12" r="9" /><path d="M12 11v6" /><path d="M12 7.5v.5" /></>,
  plug: <><path d="M9 3.5v4.5" /><path d="M15 3.5v4.5" /><path d="M6.5 8h11v3a5.5 5.5 0 0 1-11 0z" /><path d="M12 16.5v4" /></>,
  "plus-circle": <><circle cx="12" cy="12" r="8.5" /><path d="M12 8v8" /><path d="M8 12h8" /></>,
};

export type IconName = keyof typeof P;

export function Icon({ name, size = 16, color, width, style }: { name: IconName; size?: number; color?: string; width?: number; style?: CSSProperties }) {
  const heavy = name === "check" || name === "close";
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color ?? "currentColor"}
      strokeWidth={width ?? (heavy ? 2 : 1.6)} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"
      style={{ flexShrink: 0, ...style }}>
      {P[name]}
    </svg>
  );
}
