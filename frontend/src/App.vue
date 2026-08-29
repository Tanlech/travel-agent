<template>
  <router-view />
</template>

<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --primary: #4f6ef7; --primary-dark: #3b55d9; --primary-light: #eef1ff;
  --bg: #f5f6fa; --sidebar-text: #c7cbd6; --sidebar-grad: linear-gradient(180deg,#5d79f8 0%,#4668ec 55%,#3a56cf 100%);
  --text: #22252e; --muted: #8a8fa0; --border: #e4e7ee;
  --bubble-ai: #ffffff; --bubble-user: linear-gradient(135deg,#4f6ef7,#6a5acd);
  --danger: #e5484d; --success: #30a46c;
}
html, body { height: 100%; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  background: var(--bg); color: var(--text); font-size: 14px;
}
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-thumb { background: #cfd4e0; border-radius: 4px; }
::-webkit-scrollbar-track { background: transparent; }
.toast {
  position: fixed; left: 50%; top: 64px; transform: translateX(-50%);
  background: #fff; color: #16a34a; padding: 9px 18px; border-radius: 10px; font-size: 13px;
  box-shadow: 0 6px 18px rgba(0,0,0,.12); z-index: 200; animation: fadeIn .2s ease;
}
.toast.error { color: #dc2626; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
/* 后台内容区顶部提示容器（sticky 钉在内容区顶部，随内容区滚动） */
#app-toast-host { position: sticky; top: 0; z-index: 95; display: flex; flex-direction: column; align-items: center; pointer-events: none; margin: -6px 0 8px; }
#app-toast-host .toast { position: static; top: auto; left: auto; transform: none; margin: 0 0 8px; }

.btn {
  background: var(--primary); color: #fff; border: none; border-radius: 9px;
  padding: 9px 16px; font-size: 13px; cursor: pointer; transition: background .2s;
}
.btn:hover { background: var(--primary-dark); }
.btn.ghost { background: #fff; color: var(--text); border: 1px solid var(--border); }
.btn.ghost:hover { background: var(--primary-light); border-color: var(--primary); }
.btn.danger { background: #fff; color: var(--danger); border: 1px solid #f1c3c4; }
.btn.danger:hover { background: #fdecec; }
.btn.mini { padding: 5px 10px; font-size: 12px; border-radius: 7px; }

.modal-mask {
  position: fixed; inset: 0; background: rgba(20,22,30,.45);
  display: flex; align-items: center; justify-content: center; z-index: 100; padding: 20px;
}
.modal {
  background: #fff; border-radius: 14px; width: min(760px, 94vw);
  max-height: 86vh; display: flex; flex-direction: column; overflow: hidden;
  box-shadow: 0 16px 48px rgba(0,0,0,.24); animation: fadeIn .18s ease;
}
.modal.modal-sm { width: min(420px, 92vw); }
.modal .m-head { display: flex; align-items: center; gap: 10px; padding: 14px 18px; border-bottom: 1px solid var(--border); flex-shrink: 0; }
.modal .m-head h2 { font-size: 15px; font-weight: 700; flex: 1; }
.modal .m-close { border: none; background: var(--primary-light); color: var(--primary); width: 28px; height: 28px; border-radius: 8px; cursor: pointer; font-size: 14px; line-height: 1; transition: background .2s; }
.modal .m-close:hover { background: #e2e8ff; }
.modal .m-body { flex: 1; overflow-y: auto; padding: 16px 18px; font-size: 13px; line-height: 1.75; }
.modal .m-foot { display: flex; justify-content: flex-end; align-items: center; gap: 10px; padding: 12px 18px; border-top: 1px solid var(--border); flex-shrink: 0; }
.modal .m-sec { font-size: 13px; font-weight: 700; color: #22252e; margin: 14px 0 8px; }
.modal .m-row { padding: 10px 0; border-bottom: 1px dashed var(--border); }
.modal .m-row:last-child { border-bottom: none; }
.modal .m-meta { color: var(--muted); font-size: 11px; margin-bottom: 3px; }
.modal .m-text { white-space: pre-wrap; word-break: break-word; }
.empty { color: var(--muted); font-size: 13px; padding: 10px 0; }
</style>
