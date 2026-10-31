/* 凭据卡片：B 站 / 小红书 / Pixiv / GitHub 的统一凭据入口。
 *
 * 目标：把「粘贴一长串 Cookie / Token」这件最容易出错的事收拢到一个地方 ——
 * 平台选好、内容粘贴或从文件导入、导入即保存，再提供「检查当前凭据」和
 * 「清除」两个动作。用户在这里永远只需要回答两个问题：我有没有配好？还能用吗？
 *
 * 接口契约（旧后端不可用时回退到配置输入框）：
 *   GET  credentials        -> { platforms: [ {id,label,kind,configured,source,status,message,checked_at} ] }
 *   POST credentials/import <- { platform, content, format:"auto" } -> 同 GET 形状
 *   POST credentials/check  <- { platform } -> 同 GET 形状
 *   POST credentials/clear  <- { platform } -> 同 GET 形状
 *
 * 两条必须守住的红线：
 * 1. **导入成功 ≠ 登录有效。** import 只是把内容存下来，status 仍是
 *    configured / unknown，要等 check 才能变 valid / invalid。界面不替后端
 *    乐观升级状态，否则用户会拿着一个已失效的 Cookie 去排查别的问题。
 * 2. **不整页重绘。** 粘贴区要能连续输入，导入失败后要能直接改内容重试，
 *    所以状态更新只改文本 / disabled，不重建任何输入节点。
 *
 * 放在 pages/denia/ 而不是 views/：views/ 下的模块按约定都是「左侧导航里的一个
 * 页面」，由 app.js 统一 import（tests/frontend/asset-contract.test.mjs 会把
 * views/ 里没人引用的文件判成孤儿模块）。这里是配置页的内部组件，与 ui.js 同级。
 */

import { h, clear, card, toast, confirmDialog, withBusy } from "./ui.js";
import { createMusicLogin } from "./music_login.js";

/** 平台 id 顺序与后端保持一致：前端只按后端给的 platforms 渲染，这份列表只用于兜底提示。 */
export const PLATFORM_IDS = ["bilibili", "xiaohongshu", "pixiv", "github", "netease", "qqmusic", "xiaoheihe"];

/** 凭据管理器接管的配置键。API 可用后这些键不再手填，避免两处入口互相覆盖。 */
export const CREDENTIAL_KEYS = {
  BILI_CK: "bilibili",
  XHS_CK: "xiaohongshu",
  PIXIV_CK: "pixiv",
  GITHUB_TOKEN: "github",
  NETEASE_CK: "netease",
  QQMUSIC_CK: "qqmusic",
  XIAOHEIHE_CK: "xiaoheihe",
};

/* status -> [中文, pill 样式]。未知值一律落到「状态未知」，不吞掉。 */
const STATUS_TEXT = {
  unconfigured: ["未配置", "warn"],
  configured: ["已配置", ""],
  valid: ["可用", "ok"],
  invalid: ["已失效", "err"],
  unknown: ["状态未知", ""],
  error: ["检查出错", "err"],
};

const KIND_LABEL = { cookie: "Cookie", token: "Token" };
const SOURCE_LABEL = { none: "尚未设置", config: "配置保存", qrcode: "扫码登录", memory: "当前登录态", cookiecloud: "CookieCloud 同步" };
const MAX_IMPORT_BYTES = 256 * 1024;

function statusOf(platform) {
  return STATUS_TEXT[platform?.status] || STATUS_TEXT.unknown;
}

/** ISO 时间 -> 「YYYY-MM-DD HH:MM」，解析不了就原样返回。 */
function fmtCheckedAt(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const pad = (n) => String(n).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

/** 平台专属的获取指引，替代暴露实现细节的技术文案。换行用 \n，样式按 pre-line 渲染。 */
function platformTip(platform) {
  if (platform.id === "netease") {
    return (
      "获取步骤：\n" +
      "1. 电脑浏览器打开 music.163.com 并登录（扫码或账号均可）；\n" +
      "2. 按 F12 打开开发者工具，切到「网络 / Network」标签，按 F5 刷新页面；\n" +
      "3. 点列表里第一条 music.163.com 请求，在「请求标头 / Request Headers」里找到 Cookie，整串复制粘贴到上面；\n" +
      "4. 或在「应用 / Application → Cookie → music.163.com」里只复制 MUSIC_U 的值，粘贴为 MUSIC_U=值。\n" +
      "会员歌曲按账号实际权益获取；扫码登录已下线（网易已封禁第三方扫码），Cookie 导入是唯一方式。"
    );
  }
  if (platform.id === "qqmusic") {
    return (
      "推荐：点上方「扫码登录」用手机 QQ 扫码，保存的凭据支持自动续期。\n" +
      "手动导入步骤：\n" +
      "1. 电脑浏览器打开 y.qq.com 并登录（手机 QQ 扫码即可）；\n" +
      "2. 按 F12 →「网络 / Network」→ 刷新页面 → 点任一条 y.qq.com 请求；\n" +
      "3. 在「请求标头」的 Cookie 里找到 uin 和 qm_keyst（或 qqmusic_key）两段；\n" +
      "4. 粘贴为 uin=值; qm_keyst=值（分号分隔），或整串 Cookie 直接粘贴。\n" +
      "只导入 Cookie 无法自动续期，过期后需重新导入。"
    );
  }
  if (platform.id === "bilibili") {
    return (
      "获取步骤：\n" +
      "1. 电脑浏览器登录 bilibili.com；\n" +
      "2. F12 →「网络 / Network」→ 刷新页面 → 点第一条 bilibili.com 请求；\n" +
      "3. 在「请求标头 / Request Headers」里把 Cookie 整串复制粘贴到上面（必须包含 SESSDATA）。\n" +
      "也可以在本页或「总览」页直接扫码登录；当前实际生效的是哪一份，请看上方的生效来源。"
    );
  }
  if (platform.id === "xiaohongshu") {
    return (
      "获取步骤：\n" +
      "1. 电脑浏览器登录 xiaohongshu.com；\n" +
      "2. F12 →「网络 / Network」→ 刷新页面 → 点任一条 xiaohongshu.com 请求；\n" +
      "3. 在「请求标头」的 Cookie 整串复制粘贴（需包含 web_session）。"
    );
  }
  if (platform.id === "pixiv") {
    return (
      "获取步骤：\n" +
      "1. 电脑浏览器登录 pixiv.net（能正常看到登录态即可）；\n" +
      "2. F12 →「网络 / Network」→ 刷新页面 → 点任一条 pixiv.net 请求；\n" +
      "3. 在「请求标头」的 Cookie 整串复制粘贴（需包含 PHPSESSID）。"
    );
  }
  if (platform.id === "xiaoheihe") {
    return (
      "获取步骤：\n" +
      "1. 电脑浏览器登录 xiaoheihe.cn（小黑盒网页版）；\n" +
      "2. F12 →「网络 / Network」→ 刷新页面 → 点任一条请求；\n" +
      "3. 在「请求标头」的 Cookie 整串复制粘贴到上面。\n" +
      "平台风控较严：部分内容要求先在小黑盒网页里完成一次验证。"
    );
  }
  if (platform.id === "github") {
    return (
      "这里要的是 Token，不是 Cookie。获取步骤：\n" +
      "1. 登录 GitHub → 右上头像 → Settings → 左栏最底部 Developer settings；\n" +
      "2. Personal access tokens → Tokens (classic) → Generate new token (classic)；\n" +
      "3. 备注随便填，权限一项都不勾也可以（读公开仓库够用）；\n" +
      "4. 生成后立即复制粘贴到上面——Token 只显示这一次。"
    );
  }
  const isToken = platform.kind === "token";
  return `支持浏览器里复制的原始 ${isToken ? "Token" : "Cookie"}、请求头、cURL 命令、JSON 或 cookies.txt 文件。`;
}

/**
 * 建凭据卡片。
 * @param {object} ctx 页面上下文（只用 ctx.api）
 * @param {{onChange?: (platforms:any[])=>void}} [hooks] 状态变化回调（供配置页同步提示文案）
 */
export function createCredentialsPanel(ctx, hooks = {}) {
  let container = null;
  let platforms = [];
  let selected = PLATFORM_IDS[0];
  let supported = false;
  let busy = false;
  /** 失败后的可重试提示；文案里不出现接口名、字段名这类实现细节。 */
  let lastError = "";
  const inputDrafts = new Map();
  const musicLogin = createMusicLogin(ctx, async () => {
    applyResult(await ctx.api.get("credentials"));
  });

  /* ---- 常驻节点引用：状态更新只改它们的文本 / disabled，不重建 ---- */
  const tabsHost = h("div", { class: "seg cred-tabs" });
  const detail = h("div", { class: "cred-detail" });
  const errorNode = h("div", { class: "cred-error", role: "alert" });
  const paste = h("textarea", {
    class: "input cred-paste",
    rows: 3,
    spellcheck: "false",
    dataset: { credPaste: "1" },
    placeholder: "把内容粘贴到这里，或从文件导入",
  });
  const fileInput = h("input", {
    class: "input cred-file",
    type: "file",
    dataset: { credFile: "1" },
  });
  paste.addEventListener("input", () => inputDrafts.set(selected, paste.value));

  function current() {
    return platforms.find((item) => item.id === selected) || null;
  }

  function labelOf(platform) {
    return platform.label || platform.id;
  }

  /** 每次状态变化后同步 DOM 引用，DOM 结构不变（重建会打断正在输入的粘贴框）。 */
  let refs = null;

  function sync() {
    if (!refs) return;
    const platform = current();
    musicLogin.select(platform?.id || "");
    const [statusLabel, statusKind] = platform ? statusOf(platform) : ["状态未知", ""];

    for (const button of tabsHost.childNodes) {
      const isActive = button.dataset && button.dataset.credPlatform === selected;
      button.classList.toggle("is-active", Boolean(isActive));
      const flag = { on: "·已配置", valid: "·可用", invalid: "·已失效" }[button.dataset.credFlag] || "·未配置";
      button.textContent = `${button.dataset.credLabel}${flag}`;
      button.disabled = busy;
    }

    const kindLabel =
      KIND_LABEL[platform?.kind] || (platform?.id === "github" ? "Token" : "Cookie");
    refs.status.textContent = statusLabel;
    refs.status.className = `pill ${statusKind}`.trim();
    refs.configured.textContent = platform?.configured ? "已配置" : "未配置";
    refs.configured.hidden = statusLabel === refs.configured.textContent;
    refs.checked.textContent = platform?.checked_at
      ? `最近检查：${fmtCheckedAt(platform.checked_at)}`
      : "最近检查：尚未检查";
    refs.source.textContent = platform?.source ? `生效来源：${SOURCE_LABEL[platform.source] || "当前登录态"}` : "";
    refs.source.hidden = !platform?.source;
    refs.message.textContent = platform?.message || "";
    refs.message.hidden = !platform?.message;
    refs.tip.textContent = platform ? platformTip(platform) : "";
    refs.pasteLabel.textContent = `粘贴${kindLabel}`;
    paste.setAttribute("aria-label", `粘贴${kindLabel}`);
    refs.paste.placeholder = platform
      ? `把${kindLabel}粘贴到这里，或从文件导入`
      : "把内容粘贴到这里，或从文件导入";

    errorNode.textContent = lastError;
    errorNode.hidden = !lastError;

    // 忙碌时三个动作一起禁用，避免并发提交把状态刷乱
    for (const button of refs.actions) button.disabled = busy || !platform;
    paste.disabled = busy;
    fileInput.disabled = busy;
    refs.pickFile.disabled = busy;
  }

  /** 三个动作都返回 GET 同形状的 platforms 列表，就地替换目标平台。 */
  function applyResult(result) {
    const list = Array.isArray(result?.platforms) ? result.platforms : null;
    if (!list || !list.length) return false;
    platforms = list;
    if (!platforms.some((item) => item.id === selected)) {
      selected = platforms[0]?.id || selected;
    }
    renderTabs();
    if (typeof hooks.onChange === "function") hooks.onChange(platforms);
    sync();
    return true;
  }

  function flagOf(platform) {
    if (!platform) return "off";
    if (platform.status === "valid" || platform.status === "invalid") return platform.status;
    return platform.configured ? "on" : "off";
  }

  function renderTabs() {
    clear(tabsHost);
    for (const platform of platforms) {
      const button = h(
        "button",
        {
          type: "button",
          class: platform.id === selected ? "is-active" : "",
          dataset: {
            credPlatform: platform.id,
            credLabel: labelOf(platform),
            credFlag: flagOf(platform),
          },
          onClick: () => {
            if (busy || selected === platform.id) return;
            inputDrafts.set(selected, paste.value);
            selected = platform.id;
            paste.value = inputDrafts.get(selected) || "";
            fileInput.value = "";
            lastError = "";
            renderTabs();
            sync();
          },
        },
        `${labelOf(platform)} · ${platform.configured ? "已配置" : "未配置"}`,
      );
      tabsHost.appendChild(button);
    }
  }

  /* ---- 三条动作 ---- */

  /** 导入内容 = 「明确导入并保存」。内容为空时直接拦下，不发请求。 */
  async function doImport(button) {
    const platform = current();
    if (!platform) return;
    const content = paste.value.trim();
    if (!content) {
      lastError = "还没有内容，先粘贴或从文件导入再点导入。";
      sync();
      return;
    }
    busy = true;
    lastError = "";
    // 先停止进行中的扫码（B站后台会取消对应任务）：否则它可能在本轮保存之后
    // 才判定成功，把刚写入/刚导入的凭据又回写一遍。
    musicLogin.cancel();
    sync();
    const restore = withBusy(button, true, "导入中…");
    try {
      // format 固定 auto：raw cookie / Header / cURL / JSON / cookies.txt 由后端判定
      const result = await ctx.api.post("credentials/import", {
        platform: platform.id,
        content,
        format: "auto",
      });
      if (!applyResult(result)) throw new Error("没有收到保存确认，请检查后重试。");
      // 成功即清空粘贴区，且不回显任何已存内容
      paste.value = "";
      inputDrafts.delete(platform.id);
      musicLogin.cancel();
      toast(`已保存 ${labelOf(platform)} 凭据`, "ok");
    } catch (error) {
      lastError = `导入失败：${error?.message || "请稍后重试"}`;
      toast(lastError, "err");
    } finally {
      restore();
      busy = false;
      sync();
    }
  }

  async function doCheck(button) {
    const platform = current();
    if (!platform) return;
    busy = true;
    lastError = "";
    sync();
    const restore = withBusy(button, true, "检查中…");
    try {
      const result = await ctx.api.post("credentials/check", { platform: platform.id });
      if (!applyResult(result)) throw new Error("没有收到检查结果，请稍后重试。");
      const [label] = statusOf(current());
      toast(`检查完成：${label}`, current()?.status === "valid" ? "ok" : "");
    } catch (error) {
      lastError = `检查失败：${error?.message || "请稍后重试"}`;
      toast(lastError, "err");
    } finally {
      restore();
      busy = false;
      sync();
    }
  }

  async function doClear(button) {
    const platform = current();
    if (!platform) return;
    const ok = await confirmDialog({
      title: `清除 ${labelOf(platform)} 凭据`,
      body: "清除后该平台需要重新登录或重新导入内容，相关平台会先退化到未登录状态。",
      confirmText: "清除",
      danger: true,
    });
    if (!ok) return;
    busy = true;
    lastError = "";
    // 清除前先停止进行中的扫码：否则任务可能在清除之后判定成功并把 Cookie 写回来。
    musicLogin.cancel();
    sync();
    const restore = withBusy(button, true, "清除中…");
    try {
      if (!applyResult(await ctx.api.post("credentials/clear", { platform: platform.id }))) {
        throw new Error("没有收到清除确认，请刷新检查。");
      }
      paste.value = "";
      inputDrafts.delete(platform.id);
      toast(`已清除 ${labelOf(platform)} 凭据`, "ok");
      musicLogin.cancel();
    } catch (error) {
      lastError = `清除失败：${error?.message || "请稍后重试"}`;
      toast(lastError, "err");
    } finally {
      restore();
      busy = false;
      sync();
    }
  }

  /* ---- 渲染 ---- */

  function build() {
    const importButton = h(
      "button",
      {
        class: "btn primary",
        type: "button",
        dataset: { credAction: "import" },
        onClick: (event) => doImport(event.currentTarget),
      },
      "导入并保存",
    );
    const checkButton = h(
      "button",
      {
        class: "btn",
        type: "button",
        dataset: { credAction: "check" },
        onClick: (event) => doCheck(event.currentTarget),
      },
      "检查当前凭据",
    );
    const clearButton = h(
      "button",
      {
        class: "btn danger",
        type: "button",
        dataset: { credAction: "clear" },
        onClick: (event) => doClear(event.currentTarget),
      },
      "清除",
    );

    const pickFile = h(
      "button",
      {
        class: "btn",
        type: "button",
        dataset: { credPickFile: "1" },
        onClick: () => {
          if (busy) return;
          fileInput.value = "";
          if (typeof fileInput.click === "function") fileInput.click();
        },
      },
      "从文件导入",
    );
    // 文件内容写进同一个粘贴框：只有一条输入路径，导入时才读它
    fileInput.addEventListener("change", () => {
      const file = fileInput.files && fileInput.files[0];
      if (!file || typeof FileReader === "undefined") return;
      if (file.size > MAX_IMPORT_BYTES) {
        lastError = "文件超过 256 KB，请只导出需要的平台凭据。";
        sync();
        return;
      }
      const platformId = selected;
      const reader = new FileReader();
      reader.onload = () => {
        const content = String(reader.result || "");
        inputDrafts.set(platformId, content);
        if (selected === platformId) paste.value = content;
        lastError = "";
        sync();
      };
      reader.onerror = () => {
        lastError = "这个文件读不出来，换一个文件或直接粘贴内容。";
        sync();
      };
      reader.readAsText(file);
    });

    refs = {
      status: h("span", { class: "pill", dataset: { credStatus: "1" } }),
      configured: h("span", { class: "cred-meta", dataset: { credConfigured: "1" } }),
      checked: h("span", { class: "cred-meta", dataset: { credChecked: "1" } }),
      source: h("span", { class: "cred-meta", dataset: { credSource: "1" }, hidden: true }),
      message: h("p", { class: "card-note", dataset: { credMessage: "1" }, hidden: true }),
      tip: h("p", { class: "field-hint", dataset: { credTip: "1" } }),
      pasteLabel: h("span", { class: "field-label", dataset: { credPasteLabel: "1" } }, "粘贴内容"),
      paste,
      pickFile,
      actions: [importButton, checkButton, clearButton],
    };
    paste.id = "cred-paste";

    detail.appendChild(
      h("div", { class: "cred-status-row" }, [
        refs.status,
        refs.configured,
        refs.checked,
        refs.source,
      ]),
    );
    detail.appendChild(refs.message);
    detail.appendChild(refs.tip);
    detail.appendChild(
      h("div", { class: "field cred-paste-field" }, [
        h("div", { class: "field-body" }, [
          h("div", { class: "inline grow" }, [paste]),
          h("div", { class: "inline" }, [pickFile, fileInput]),
        ]),
      ]),
    );
    detail.appendChild(
      h("div", { class: "cred-actions" }, [
        importButton,
        checkButton,
        clearButton,
        h("span", { class: "grow" }),
        h("span", {
          class: "field-hint",
          text: "导入会立即保存；检查只验证当前凭据是否还能用。",
        }),
      ]),
    );
    detail.appendChild(errorNode);
    detail.appendChild(musicLogin.node);
    sync();
    return detail;
  }

  function buildNode() {
    // build() 往常驻的 detail 里铺控件；只铺一次，之后 sync() 只改文本不重建
    build();
    const body = h("div", {}, [tabsHost, detail]);
    const node = card(
      "账号凭据",
      "统一管理平台 Cookie、Token 与扫码登录",
      [body],
    );
    node.classList.add("cfg-credentials");
    return node;
  }

  /* ---- 对外 ---- */

  const node = buildNode();

  return {
    node,
    cancelLogin: () => musicLogin.cancel(),
    get supported() {
      return supported;
    },
    platforms: () => platforms,
    /** 该配置键是否已被凭据管理器接管（接管后不再提供手填入口）。 */
    managesKey(key) {
      return supported && Object.prototype.hasOwnProperty.call(CREDENTIAL_KEYS, key);
    },
    platformOfKey(key) {
      return CREDENTIAL_KEYS[key] || "";
    },
    sync,
    /** 读取凭据状态；后端未实现或报错时静默回退，**绝不让配置页加载失败**。 */
    async load() {
      try {
        const payload = await ctx.api.get("credentials");
        const list = Array.isArray(payload?.platforms) ? payload.platforms : null;
        supported = Boolean(list && list.length);
        if (supported) {
          platforms = list;
          if (!platforms.some((item) => item.id === selected)) {
            selected = platforms[0]?.id || selected;
          }
          renderTabs();
        }
        sync();
      } catch {
        supported = false;
      }
      return supported;
    },
  };
}

export { fmtCheckedAt };
