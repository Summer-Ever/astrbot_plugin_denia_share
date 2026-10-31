import { h, toast } from "./ui.js";

/**
 * 账号扫码登录小组件：QQ音乐 / 网易云音乐 的扫码入口，以及 B站 的扫码入口。
 *
 * B站复用总览页已有的后端（``bili/qrcode`` / ``bili/qrcode/status``），只是把
 * 入口挪到「配置 → 账号凭据」里；B站后台有自己的轮询任务，所以多一条
 * ``bili/qrcode/cancel`` 用于「停止扫码」真正取消它。
 *
 * 两条必须守住的约束：
 * 1. 页面只接触不透明 ``task_id``、PNG data URL 与状态文案，Cookie / qrcode_key
 *    一律留在服务端；
 * 2. 取消 / 切换平台 / 退出配置后，迟到的回包既不能继续 UI 轮询，也不能覆盖
 *    新一次扫码 —— 但拿到旧 ``task_id`` 时要在服务端把旧任务取消掉。
 */

const BILI_PLATFORM = "bilibili";
// 网易云已下线扫码登录（第三方扫码被网易风控/封禁），统一 Cookie 导入。
const MUSIC_PLATFORMS = ["qqmusic"];
const LOGIN_PLATFORMS = [...MUSIC_PLATFORMS, BILI_PLATFORM];
const ACTIVE_STATES = ["waiting", "scanned"];
const QR_IMAGE_RE = /^data:image\/(?:png|jpeg|webp);base64,/;
const POLL_MS = 3000;

/** Only the opaque task id and QR image reach the page; credentials stay server-side. */
export function createMusicLogin(ctx, onSuccess) {
  let platform = "";
  let generation = 0;
  let timer = null;
  let busy = false;
  /** B站当前生效的后台任务；停止扫码要按它取消，所以必须记住。 */
  let biliTaskId = "";

  const image = h("img", { class: "music-login-qr", alt: "音乐账号登录二维码", hidden: true });
  const message = h("p", { class: "field-hint", role: "status" });
  const stop = h("button", { class: "btn", type: "button", hidden: true, onClick: () => cancel() }, "停止扫码");
  const start = h("button", { class: "btn", type: "button", onClick: () => run("start") }, "扫码登录");
  const refresh = h("button", { class: "btn", type: "button", onClick: () => run("refresh") }, "续期 QQ 登录态");
  const node = h("div", { class: "music-login", hidden: true }, [
    h("div", { class: "inline" }, [start, refresh, stop]), image, message,
  ]);

  /** 取消服务端仍在轮询的 B站任务（幂等）。失败只影响省不省一次后台轮询，不回滚界面。 */
  function cancelRemote(taskId) {
    if (!taskId) return;
    try {
      const pending = ctx.api.post("bili/qrcode/cancel", { task_id: taskId });
      if (pending && typeof pending.catch === "function") pending.catch(() => {});
    } catch {
      /* 取消失败不阻塞界面复位；后台任务到点也会自己过期 */
    }
  }

  function cancel() {
    generation += 1;
    if (timer !== null) clearTimeout(timer);
    timer = null;
    const staleTask = biliTaskId;
    biliTaskId = "";
    image.hidden = true;
    image.removeAttribute("src");
    stop.hidden = true;
    message.textContent = "";
    busy = false;
    start.disabled = refresh.disabled = false;
    cancelRemote(staleTask);
  }

  function show(result) {
    message.textContent = result.message || "";
    // 音乐接口用 qr_data_url，B站接口用 data_url；状态轮询不回图片，保持原图不动。
    const qr = result.qr_data_url || result.data_url;
    if (typeof qr === "string" && QR_IMAGE_RE.test(qr)) {
      image.src = qr;
      image.hidden = false;
    }
    if (!ACTIVE_STATES.includes(result.state)) {
      image.hidden = true;
      image.removeAttribute("src");
      stop.hidden = true;
    }
  }

  async function poll(taskId, ticket) {
    if (ticket !== generation) return;
    if (node.isConnected === false) {
      cancel();
      return;
    }
    const selected = platform;
    try {
      const result =
        selected === BILI_PLATFORM
          ? await ctx.api.get("bili/qrcode/status", { task_id: taskId })
          : await ctx.api.post("music/login/poll", { task_id: taskId });
      if (ticket !== generation) return;
      show(result);
      if (result.state === "success") {
        if (selected === BILI_PLATFORM) biliTaskId = "";
        await onSuccess();
        toast(selected === BILI_PLATFORM ? "B站账号已登录" : "音乐账号已登录", "ok");
      } else if (ACTIVE_STATES.includes(result.state)) {
        timer = setTimeout(() => poll(taskId, ticket), POLL_MS);
      } else if (selected === BILI_PLATFORM) {
        biliTaskId = "";
      }
    } catch {
      if (ticket === generation) {
        image.hidden = true;
        stop.hidden = true;
        message.textContent = "扫码状态未能确认，请重新获取二维码，或使用 Cookie 导入。";
      }
    }
  }

  async function run(action) {
    if (busy || !platform) return;
    cancel();
    const ticket = generation;
    const selected = platform;
    const isBili = selected === BILI_PLATFORM;
    busy = true;
    start.disabled = refresh.disabled = true;
    message.textContent = action === "start" ? "正在获取二维码…" : "正在续期…";
    try {
      const result = isBili
        ? await ctx.api.post("bili/qrcode", {})
        : await ctx.api.post(`music/login/${action}`, { platform: selected });
      if (ticket !== generation) {
        // 界面已经取消 / 切走：把刚拿到的 B站任务也在服务端停掉。按 task_id 取消，
        // 绝不会碰到之后新建的任务（task_id 不重叠）。
        if (isBili && action === "start" && result?.task_id !== biliTaskId) {
          cancelRemote(result?.task_id);
        }
        return;
      }
      if (isBili) biliTaskId = action === "start" ? result.task_id || "" : "";
      show(result);
      if (result.state === "success") {
        await onSuccess();
        toast(isBili ? "B站登录态已更新" : "音乐登录态已更新", "ok");
      } else if (action === "start" && result.task_id && ACTIVE_STATES.includes(result.state)) {
        stop.hidden = false;
        timer = setTimeout(() => poll(result.task_id, ticket), POLL_MS);
      }
    } catch {
      if (ticket === generation) message.textContent = "登录操作未完成，请重试或使用 Cookie 导入。";
    } finally {
      if (ticket === generation) {
        busy = false;
        start.disabled = refresh.disabled = false;
      }
    }
  }

  return {
    node,
    cancel,
    select(value) {
      if (platform === value) return;
      cancel();
      platform = value;
      node.hidden = !LOGIN_PLATFORMS.includes(platform);
      // 只有 QQ音乐 支持续期，网易云与 B站 都不显示这个按钮。
      refresh.hidden = platform !== "qqmusic";
      image.alt = platform === BILI_PLATFORM ? "B站登录二维码" : "音乐账号登录二维码";
    },
  };
}
