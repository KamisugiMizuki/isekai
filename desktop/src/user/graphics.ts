/*
 * 图形化基础件：把「一串关系 / 一段比例 / 一条流程」从文字里拿出来画出来。
 *
 * 只用 DOM + SVG，不引依赖。三条纪律（§10.2 与 ui-critique-remediation）：
 *   - 图是**补充**不是替代：每张图旁边保留可读的文字，读屏不吃亏；
 *   - 颜色只做强调，含义写在文字与 title 里（不只靠颜色）；
 *   - 不画空数据：算不出比例就返回 null，调用方按原有文案说「还没有…」。
 */

const SVG_NS = "http://www.w3.org/2000/svg";

function s<K extends keyof SVGElementTagNameMap>(
  tag: K,
  attrs: Record<string, string | number> = {},
  ...children: Array<Node | string>
): SVGElementTagNameMap[K] {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  for (const child of children) node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  return node;
}

function box(cls: string, ...children: Array<Node | string | null | undefined>): HTMLElement {
  const node = document.createElement("div");
  node.className = cls;
  for (const child of children) if (child) node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  return node;
}

/* ------------------------------------------------------------------ 流程轨 */

export interface FlowStep {
  label: string;
  hint?: string;
}

/**
 * 流程轨：几个阶段排成一条，走过的打勾、当前那格反白、没到的置灰。
 * `current = -1` 表示只列顺序、不标当前位置（说明性的层次链）。
 * 越界（≥ 长度）返回 null：画不出来就别画一张会撒谎的图，调用方保留原文字。
 */
export function flowRail(steps: FlowStep[], current: number): HTMLElement | null {
  if (!steps.length || current >= steps.length) return null;
  const rail = box("u-rail");
  steps.forEach((step, index) => {
    const done = current >= 0 && index < current;
    const state = index === current ? "current" : done ? "done" : "todo";
    const node = box(`u-rail-step u-rail-${state}`);
    node.appendChild(box("u-rail-dot", done ? "✓" : String(index + 1)));
    node.appendChild(box("u-rail-label", step.label));
    if (step.hint) node.title = step.hint;
    if (index === current) node.setAttribute("aria-current", "step");
    rail.appendChild(node);
  });
  return rail;
}

/* ------------------------------------------------------------------ 比例条 */

export interface Seg {
  label: string;
  value: number;
  tone?: "ok" | "bad" | "warn" | "muted" | "pending" | "accent";
}

/** 比例条：按值分段占宽；图例带数值——不看颜色也能读。全 0 返回 null。 */
export function stackBar(segs: Seg[], opts: { legend?: boolean } = {}): HTMLElement | null {
  const shown = segs.filter((item) => item.value > 0);
  const total = shown.reduce((sum, item) => sum + item.value, 0);
  if (!total) return null;
  const wrap = box("u-bar-wrap");
  const bar = box("u-bar");
  bar.setAttribute("role", "img");
  bar.setAttribute("aria-label", shown.map((item) => `${item.label} ${item.value}`).join("、"));
  for (const item of shown) {
    const seg = box(`u-bar-seg u-tone-${item.tone ?? "accent"}`);
    seg.style.width = `${(item.value / total) * 100}%`;
    seg.title = `${item.label}：${item.value}`;
    bar.appendChild(seg);
  }
  wrap.appendChild(bar);
  if (opts.legend !== false) {
    const legend = box("u-bar-legend");
    for (const item of shown) {
      legend.appendChild(
        box("u-bar-key", box(`u-bar-swatch u-tone-${item.tone ?? "accent"}`), `${item.label} ${item.value}`),
      );
    }
    wrap.appendChild(legend);
  }
  return wrap;
}

/** 计量条：已用 / 上限，带百分比。上限 ≤ 0（没设上限）就只报读数。 */
export function meter(used: number, limit: number, label = ""): HTMLElement {
  const ratio = limit > 0 ? Math.min(1, Math.max(0, used / limit)) : 0;
  const pct = limit > 0 ? Math.round(ratio * 100) : 0;
  const wrap = box("u-meter");
  const head = box("u-meter-head");
  head.appendChild(box("u-meter-label", label));
  head.appendChild(
    box("u-meter-value", limit > 0 ? `${Math.round(used)} / ${Math.round(limit)}（${pct}%）` : `${Math.round(used)}`),
  );
  const bar = box("u-bar");
  const seg = box(`u-bar-seg ${ratio >= 0.9 ? "u-tone-bad" : ratio >= 0.6 ? "u-tone-warn" : "u-tone-accent"}`);
  seg.style.width = `${limit > 0 ? ratio * 100 : 0}%`;
  bar.appendChild(seg);
  wrap.appendChild(head);
  wrap.appendChild(bar);
  if (limit > 0) wrap.title = `${label}：已用 ${Math.round(used)}，上限 ${Math.round(limit)}`;
  return wrap;
}

/* ------------------------------------------------------------------ 分支图 */

export interface BranchCommit {
  id: string;
  /** 世界时刻（世界秒） */
  moment: number;
  kind: string;
  title: string;
}

export interface BranchLane {
  id: string;
  name: string;
  state: string;
  commits: BranchCommit[];
  /** 从哪条提交分出来的（提交 id）；主线空 */
  source?: string;
}

const LANE_STEP = 34;
const DOT_STEP = 26;
const GUTTER = 170;
const PAD = 18;
const TOP = 22;

/**
 * 分支图：一条时间线一条泳道，提交是圆点，分叉用虚线连回来源提交。
 * 平铺的版本列表看不出「这条线从哪儿长出来的」——那张关系只有画出来才读得懂。
 */
export function branchGraph(lanes: BranchLane[]): HTMLElement | null {
  const withCommits = lanes.filter((lane) => lane.commits.length);
  if (!withCommits.length) return null;

  // 横向位置按「全局提交顺序」定：跨线可比、点不会重叠（时间戳会挤在一起）
  const order = new Map<string, number>();
  lanes
    .flatMap((lane) => lane.commits.map((commit) => ({ lane, commit })))
    .sort((a, b) => a.commit.moment - b.commit.moment || a.commit.id.localeCompare(b.commit.id))
    .forEach((item, index) => order.set(item.commit.id, index));
  const columns = order.size;

  const width = GUTTER + Math.max(columns, 2) * DOT_STEP + PAD;
  const height = TOP + lanes.length * LANE_STEP + PAD;
  const svg = s("svg", {
    class: "u-branch",
    viewBox: `0 0 ${width} ${height}`,
    // 按原尺寸画，不拉满容器：viewBox 拉伸会把里面的字一起放大（点少的时候字会有三倍大）
    style: `width:${width}px`,
    role: "img",
    "aria-label": `版本分叉图：${lanes.length} 条时间线、${columns} 个版本点`,
  });

  const xOf = (id: string): number => GUTTER + (order.get(id) ?? 0) * DOT_STEP + DOT_STEP / 2;
  const yOf = (index: number): number => TOP + index * LANE_STEP;

  lanes.forEach((lane, index) => {
    const y = yOf(index);
    const xs = lane.commits.map((commit) => xOf(commit.id));
    const start = xs.length ? Math.min(...xs) : GUTTER;
    const end = xs.length ? Math.max(...xs) : GUTTER + 40;
    svg.appendChild(
      s("line", {
        x1: start, x2: end, y1: y, y2: y,
        "stroke-width": 1.5, "stroke-linecap": "round",
        class: lane.state === "active" ? "u-edge u-edge-live" : "u-edge",
      }),
    );
    const label = s("text", { x: 6, y: y + 4, class: "u-branch-label" });
    label.appendChild(s("title", {}, lane.name));
    label.textContent = `${lane.name.length > 11 ? `${lane.name.slice(0, 11)}…` : lane.name}`;
    svg.appendChild(label);
    const stateText = lane.state === "active" ? "运行中" : lane.state === "frozen" ? "已暂停" : "已归档";
    svg.appendChild(s("text", { x: 6, y: y + 16, class: "u-branch-sub" }, stateText));
    if (!lane.commits.length) {
      svg.appendChild(s("text", { x: start + 8, y: y + 4, class: "u-branch-sub" }, "还没有版本点"));
    }
  });

  // 分叉：从来源提交画一条虚线到子线的第一个提交（主线的来源是它自己的初始化提交，不算分叉）
  const byId = new Map<string, { lane: number; commit: BranchCommit }>();
  lanes.forEach((lane, index) => lane.commits.forEach((commit) => byId.set(commit.id, { lane: index, commit })));
  lanes.forEach((lane, index) => {
    const source = lane.source ? byId.get(lane.source) : undefined;
    if (!source || source.lane === index || !lane.commits.length) return;
    const from = { x: xOf(source.commit.id), y: yOf(source.lane) };
    const to = { x: xOf(lane.commits[0].id), y: yOf(index) };
    const midX = from.x + (to.x - from.x) * 0.55;
    svg.appendChild(
      s("path", {
        d: `M ${from.x} ${from.y} C ${midX} ${from.y} ${midX} ${to.y} ${to.x} ${to.y}`,
        fill: "none", "stroke-width": 1.5, "stroke-dasharray": "4 3",
        class: "u-edge u-edge-branch",
      }),
    );
  });

  // 提交点：点一下把下面的列表滚到对应那一行（图与操作面用同一份数据）
  lanes.forEach((lane, index) => {
    const y = yOf(index);
    const sorted = lane.commits.map((commit, position) => ({ commit, last: position === lane.commits.length - 1 }));
    for (const { commit, last } of sorted) {
      const dot = s("circle", {
        cx: xOf(commit.id), cy: y, r: last ? 5.5 : 4,
        class: `u-node ${commit.kind === "initial" ? "u-node-initial" : ""} ${last ? "u-node-head" : ""}`,
      });
      dot.appendChild(s("title", {}, `${commit.title}｜世界进度 ${commit.moment}`));
      dot.style.cursor = "pointer";
      dot.addEventListener("click", () => {
        const row = document.querySelector(`[data-commit-row="${commit.id}"]`);
        if (!(row instanceof HTMLElement)) return;
        row.scrollIntoView({ block: "center", behavior: "smooth" });
        row.classList.add("u-commit-hit");
        window.setTimeout(() => row.classList.remove("u-commit-hit"), 1600);
      });
      svg.appendChild(dot);
    }
  });

  return box("u-branch-wrap", svg);
}

/* ------------------------------------------------------------------ 状态点 */

/** 一行字前面的状态点：颜色只是强调，文字本身说清了状态 */
export function dotLine(text: string, tone: "ok" | "pending" | "bad" | "muted" = "muted"): HTMLElement {
  return box("u-dot-line", box(`u-dot u-tone-${tone}`), box("u-dot-text", text));
}

/** 检查项清单（本机检查 / 连接测试共用）：✓ / ✗ + 说明，不用「通过」两字去猜哪项没过 */
export function checkList(items: Array<{ label: string; ok: boolean; detail?: string }>): HTMLElement {
  const list = box("u-checks");
  for (const item of items) {
    list.appendChild(
      box(
        "u-check-line",
        box(`u-check-glyph ${item.ok ? "u-tone-ok" : "u-tone-bad"}`, item.ok ? "✓" : "✗"),
        box("u-check-label", item.label),
        item.detail ? box("u-check-detail", item.detail) : null,
      ),
    );
  }
  return list;
}
