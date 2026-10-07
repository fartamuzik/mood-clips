// Будильник для клип-бота (Cloudflare Worker).
// Раз в минуту смотрит, есть ли новые сообщения боту в Telegram, и если есть —
// запускает GitHub Actions. Сам ничего не обрабатывает и сообщения не трогает.
//
// Переменные (Settings → Variables and Secrets):
//   TG_TOKEN — токен бота от BotFather (Secret)
//   GH_TOKEN — токен GitHub с правом Actions: Read and write (Secret)
//   GH_REPO  — ник/репозиторий, например  maks/mood-clips  (Text)
// Триггер (Settings → Triggers → Cron): * * * * *

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(check(env).then((r) => console.log(r)));
  },
  // Открой адрес воркера в браузере — увидишь, что он видит прямо сейчас.
  async fetch(request, env) {
    return new Response(await check(env), {
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  },
};

async function check(env) {
  const need = ["TG_TOKEN", "GH_TOKEN", "GH_REPO"].filter((k) => !env[k]);
  if (need.length) return "Не заданы переменные: " + need.join(", ");

  const tg = await fetch(
    `https://api.telegram.org/bot${env.TG_TOKEN.trim()}/getUpdates?timeout=0&limit=1`
  );
  const tj = await tg.json().catch(() => ({}));
  if (!tj.ok) return "Telegram: " + (tj.description || tg.status);
  if (!tj.result || !tj.result.length) return "Новых сообщений нет";

  const repo = env.GH_REPO.trim()
    .replace(/^https?:\/\/github\.com\//, "")
    .replace(/\.git$/, "")
    .replace(/\/+$/, "");
  const wf = env.GH_WORKFLOW || "clips.yml";
  const gh = {
    Authorization: `Bearer ${env.GH_TOKEN.trim()}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "clip-bot-trigger",
  };

  const runs = await fetch(
    `https://api.github.com/repos/${repo}/actions/workflows/${wf}/runs?per_page=5`,
    { headers: gh }
  );
  const rj = await runs.json().catch(() => ({}));
  if (!runs.ok) return `GitHub (список запусков): ${runs.status} ${rj.message || ""}`;
  const now = Date.now();
  for (const r of rj.workflow_runs || []) {
    if (r.status !== "completed") return `Бот уже работает (${r.status}) — жду`;
    if (now - Date.parse(r.created_at) < 90 * 1000) return "Бот запускался меньше 1,5 минут назад — жду";
  }

  const d = await fetch(
    `https://api.github.com/repos/${repo}/actions/workflows/${wf}/dispatches`,
    {
      method: "POST",
      headers: { ...gh, "Content-Type": "application/json" },
      body: JSON.stringify({ ref: env.GH_BRANCH || "main" }),
    }
  );
  if (d.status === 204) return "Есть новое сообщение — запустил бота";
  return `GitHub (запуск): ${d.status} ${await d.text()}`;
}
