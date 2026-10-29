/**
 * Палитра статусов на основе руководства по фирменному стилю In.Plan.
 *
 * Цвета правил условного форматирования задаются в настройках экрана свободно
 * (исторически — палитра Material). При отрисовке цвет приводится к фирменному
 * той же смысловой группы и той же насыщенности, чтобы экраны выглядели в одном стиле,
 * а смысл раскраски (норма / внимание / проблема) сохранялся. Данные не меняются.
 */

type BrandGroup = 'norm' | 'warning' | 'problem' | 'accent' | 'neutral';

/** Фон: светлый, средний, насыщенный оттенок группы; текст ячейки остаётся читаемым */
const BACKGROUND: Record<BrandGroup, readonly [string, string, string]> = {
  // голубой из градиента категорий руководства (#5ee2ff → #00576a); приглушён:
  // «норма» заливает целые листы и не должна спорить с сигналами внимания и проблемы
  norm: ['#eefbfe', '#d6f5fc', '#b5ecf8'],
  // фирменный оранжевый #ff8c00
  warning: ['#fff1dc', '#ffd9a3', '#ffb54d'],
  // градиент «Проблема» руководства (#e6779a → #ff0017)
  problem: ['#fde4ec', '#f5b3c6', '#e6779a'],
  // фирменный синий #4769ff
  accent: ['#e3e9ff', '#c2cfff', '#91a6ff'],
  // фон рамки #f4f6fa и серый #a1a9b7
  neutral: ['#f4f6fa', '#e3e7ee', '#c9cfda']
};

/** Цвет текста и рамок: тёмный оттенок группы */
const FOREGROUND: Record<BrandGroup, string> = {
  norm: '#00576a',
  warning: '#a35a00',
  problem: '#c4002a',
  accent: '#4769ff',
  neutral: '#04143a'
};

const COLOR_KEYS = new Set(['backgroundColor', 'background-color', 'background']);
const FOREGROUND_KEYS = new Set(['color', 'borderColor', 'border-color']);

const cache = new Map<string, string>();

function parseColor(value: string): [number, number, number] | null {
  const v = value.trim().toLowerCase();
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/.exec(v);
  if (hex) {
    const h = hex[1].length === 3 ? [...hex[1]].map(c => c + c).join('') : hex[1];
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
  }
  const rgb = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/.exec(v);
  return rgb ? [+rgb[1], +rgb[2], +rgb[3]] : null;
}

function toHsl([r, g, b]: [number, number, number]): [number, number, number] {
  const [rn, gn, bn] = [r / 255, g / 255, b / 255];
  const max = Math.max(rn, gn, bn);
  const min = Math.min(rn, gn, bn);
  const l = (max + min) / 2;
  const d = max - min;
  if (d === 0) return [0, 0, l * 100];
  const s = d / (1 - Math.abs(2 * l - 1));
  let h = max === rn ? ((gn - bn) / d) % 6 : max === gn ? (bn - rn) / d + 2 : (rn - gn) / d + 4;
  h = (h * 60 + 360) % 360;
  return [h, s * 100, l * 100];
}

function groupOf(h: number, s: number, l: number): BrandGroup {
  if (s < 15 || l > 97) return 'neutral';
  if (h < 15 || h >= 330) return 'problem';
  if (h < 70) return 'warning';
  if (h < 200) return 'norm';
  return 'accent';
}

/** Возвращает фирменный цвет той же смысловой группы; нераспознанное значение — без изменений */
export function toBrandColor(value: string, role: 'background' | 'foreground'): string {
  const key = role + value;
  const cached = cache.get(key);
  if (cached) return cached;
  const rgb = parseColor(value);
  let result = value;
  if (rgb) {
    const [h, s, l] = toHsl(rgb);
    const group = groupOf(h, s, l);
    if (role === 'foreground') {
      result = FOREGROUND[group];
    } else {
      // Оранжевый в «светофоре» строже жёлтого — на ступень насыщеннее
      const shift = group === 'warning' && h < 40 ? 1 : 0;
      const tier = l >= 88 ? 0 : l >= 68 ? 1 : 2;
      result = BACKGROUND[group][Math.min(2, tier + shift)];
    }
  }
  cache.set(key, result);
  return result;
}

/** Приводит цвета в объекте стилей к фирменной палитре; прочие свойства не трогает */
export function toBrandStyles<T extends { [key: string]: string | number }>(styles: T): T {
  if (!styles) return styles;
  let changed: { [key: string]: string | number } | null = null;
  for (const [k, v] of Object.entries(styles)) {
    if (typeof v !== 'string') continue;
    const role = COLOR_KEYS.has(k) ? 'background' : FOREGROUND_KEYS.has(k) ? 'foreground' : null;
    if (!role) continue;
    const mapped = toBrandColor(v, role);
    if (mapped !== v) {
      changed ??= { ...styles };
      changed[k] = mapped;
    }
  }
  return (changed as T) ?? styles;
}
