/* 正式界面入口：先建外壳，再按核心状态决定是启动页还是工作区。 */

import "./user.css";
import "./style-accessibility.css";   // 可访问性增强（此前没接线：没有 link 也没有 import，整份规则是空转）
import { App } from "./app";

const root = document.getElementById("user-root");
if (root) {
  const app = new App(root as HTMLElement);
  (window as unknown as { __uiApp?: App }).__uiApp = app;
  (window as unknown as { __uiApi?: () => unknown }).__uiApi = () =>
    (app as unknown as { apiRef?: unknown }).apiRef ?? null;
  void app.boot();
}
