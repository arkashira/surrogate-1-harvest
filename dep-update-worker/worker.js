// ROADMAP-100 #19 — Self-hosted dependency-update PRs.
//
// Renovate/Dependabot equivalent. Runs on a Cloudflare Cron Trigger
// (weekly), walks each axentx repo, fetches dependency manifests
// (requirements.txt / package.json), checks the index for newer
// stable versions (PyPI / npm registry), and opens a single batched PR
// per repo with whatever updates exist.
//
// Why a separate Worker instead of bundling into surrogate-1-cursor:
//   - Different bindings (no D1/KV needed; we DO need GH PAT secret)
//   - Different schedule (weekly, not 5-min)
//   - Different blast radius — keeps the cursor service simple
//
// Deploy:
//   wrangler deploy --config cf-worker/dep-update-wrangler.toml
//
// Required secrets (wrangler secret put):
//   GH_PAT       — fine-grained PAT, contents:write + pull_requests:write
//                   on axentx/* + arkashira/surrogate-1-harvest
//
// Tunable env:
//   REPOS        — JSON array, defaults below
//   DRY_RUN      — "1" to log without opening PRs

const DEFAULT_REPOS = [
  "axentx/Costinel",
  "axentx/vanguard",
  "axentx/airship",
  "axentx/workio",
  "axentx/axiomops",
  "axentx/surrogate-1",
  "arkashira/surrogate-1-harvest",
];

const UA = "surrogate-1-dep-bot/1.0";

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(runAll(env));
  },
  async fetch(request, env) {
    // Manual trigger: GET /run?repo=...  or just /run for all
    const u = new URL(request.url);
    if (u.pathname === "/run" && request.method === "GET") {
      const single = u.searchParams.get("repo");
      const repos = single ? [single] : repoList(env);
      const out = await Promise.all(repos.map(r => updateRepo(r, env)));
      return new Response(JSON.stringify(out, null, 2), {
        headers: { "Content-Type": "application/json" },
      });
    }
    return new Response("dep-update-worker — POST /run or wait for cron", { status: 404 });
  },
};

function repoList(env) {
  try {
    const list = JSON.parse(env.REPOS || "[]");
    return list.length ? list : DEFAULT_REPOS;
  } catch {
    return DEFAULT_REPOS;
  }
}

async function runAll(env) {
  const repos = repoList(env);
  const results = [];
  for (const repo of repos) {
    try {
      results.push(await updateRepo(repo, env));
    } catch (e) {
      results.push({ repo, error: e.message });
    }
  }
  return results;
}

async function updateRepo(repo, env) {
  const dryRun = env.DRY_RUN === "1";
  const headers = ghHeaders(env);

  const defaultBranch = await ghDefaultBranch(repo, headers);
  const summary = { repo, branch: defaultBranch, manifests: [], updates: [], pr: null };

  // Try Python + JS manifests in common locations
  const candidates = [
    "requirements.txt",
    "pyproject.toml",
    "package.json",
    "cf-worker/package.json",
  ];

  const updates = [];
  const originals = {};
  for (const path of candidates) {
    const file = await ghGetFile(repo, path, defaultBranch, headers);
    if (!file) continue;
    summary.manifests.push(path);
    originals[path] = file.text;
    if (path.endsWith("requirements.txt")) {
      updates.push(...await checkPythonReqs(file.text, path));
    } else if (path.endsWith("package.json")) {
      updates.push(...await checkNpmDeps(file.text, path));
    }
  }

  if (!updates.length) {
    summary.message = "no updates";
    return summary;
  }

  summary.updates = updates;
  if (dryRun) {
    summary.pr = "DRY_RUN";
    return summary;
  }

  // Branch + commits + PR
  const branchName = `bot/dep-updates-${weekStamp()}`;
  await ghEnsureBranch(repo, branchName, defaultBranch, headers);

  const filesChanged = applyUpdates(updates, originals);
  for (const [path, body] of Object.entries(filesChanged)) {
    await ghPutFile(repo, path, body, branchName,
      `chore(deps): bump ${updates.filter(u => u.path === path).length} package(s)`,
      headers);
  }

  const pr = await ghOpenPr(repo, branchName, defaultBranch, prBody(updates), headers);
  summary.pr = pr.html_url;
  return summary;
}

// ── PyPI ────────────────────────────────────────────────────────────────────
async function checkPythonReqs(text, path) {
  const out = [];
  const lines = text.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const m = line.match(/^([a-zA-Z0-9_.\-]+)\s*([=<>!~]{1,2})\s*([0-9][0-9a-zA-Z.\-+]*)\s*$/);
    if (!m) continue;
    const [, pkg, op, current] = m;
    if (op !== "==") continue; // only bump pinned versions
    const latest = await pypiLatest(pkg);
    if (latest && cmpVer(latest, current) > 0) {
      out.push({ ecosystem: "pypi", path, line: i, pkg, current, latest });
    }
  }
  return out;
}

async function pypiLatest(pkg) {
  const r = await fetch(`https://pypi.org/pypi/${encodeURIComponent(pkg)}/json`, {
    headers: { "User-Agent": UA },
  });
  if (!r.ok) return null;
  const d = await r.json();
  return d.info?.version || null;
}

// ── npm ─────────────────────────────────────────────────────────────────────
async function checkNpmDeps(text, path) {
  const out = [];
  let pkg;
  try { pkg = JSON.parse(text); } catch { return out; }
  for (const section of ["dependencies", "devDependencies"]) {
    const deps = pkg[section] || {};
    for (const [name, spec] of Object.entries(deps)) {
      const cur = (spec || "").replace(/^[\^~>=<]+/, "").trim();
      if (!/^\d/.test(cur)) continue;
      const latest = await npmLatest(name);
      if (latest && cmpVer(latest, cur) > 0) {
        out.push({ ecosystem: "npm", section, path, pkg: name, current: cur, latest });
      }
    }
  }
  return out;
}

async function npmLatest(name) {
  const r = await fetch(`https://registry.npmjs.org/${encodeURIComponent(name)}/latest`, {
    headers: { "User-Agent": UA, Accept: "application/json" },
  });
  if (!r.ok) return null;
  const d = await r.json();
  return d.version || null;
}

// ── Apply patches ───────────────────────────────────────────────────────────
// Rewrites manifest text in-place. PyPI: replace `pkg==old` line. npm: rewrite
// the version string for each dep section, preserving range prefix (^/~/none).
function applyUpdates(updates, originals) {
  const byPath = {};
  for (const u of updates) (byPath[u.path] ||= []).push(u);
  const out = {};
  for (const [path, list] of Object.entries(byPath)) {
    const orig = originals[path];
    if (!orig) continue;
    if (path.endsWith("requirements.txt")) {
      const lines = orig.split(/\r?\n/);
      for (const u of list) {
        // Replace the literal `==current` with `==latest` on the matched line
        const pat = new RegExp(`^(\\s*${escapeRe(u.pkg)}\\s*==\\s*)${escapeRe(u.current)}(\\s*(?:#.*)?)$`);
        const i = u.line ?? lines.findIndex(l => pat.test(l));
        if (i >= 0 && pat.test(lines[i])) {
          lines[i] = lines[i].replace(pat, `$1${u.latest}$2`);
        }
      }
      out[path] = lines.join("\n");
    } else if (path.endsWith("package.json")) {
      const pkg = JSON.parse(orig);
      for (const u of list) {
        const sec = pkg[u.section];
        if (!sec || !sec[u.pkg]) continue;
        const prefix = (sec[u.pkg].match(/^[\^~>=<]+/) || [""])[0];
        sec[u.pkg] = `${prefix}${u.latest}`;
      }
      // Preserve trailing newline if the original had one
      out[path] = JSON.stringify(pkg, null, 2) + (orig.endsWith("\n") ? "\n" : "");
    }
  }
  return out;
}

function escapeRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

// ── Version compare (semver-ish) ───────────────────────────────────────────
function cmpVer(a, b) {
  const norm = v => v.split(/[.\-+]/).slice(0, 3).map(x => parseInt(x, 10) || 0);
  const [a1, a2, a3] = norm(a);
  const [b1, b2, b3] = norm(b);
  return (a1 - b1) || (a2 - b2) || (a3 - b3);
}

// ── GitHub API helpers ────────────────────────────────────────────────────
function ghHeaders(env) {
  return {
    Authorization: `Bearer ${env.GH_PAT}`,
    "User-Agent": UA,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
  };
}

async function ghDefaultBranch(repo, h) {
  const r = await fetch(`https://api.github.com/repos/${repo}`, { headers: h });
  if (!r.ok) throw new Error(`gh repo ${repo}: ${r.status}`);
  return (await r.json()).default_branch;
}

async function ghGetFile(repo, path, ref, h) {
  const r = await fetch(`https://api.github.com/repos/${repo}/contents/${encodeURIComponent(path)}?ref=${ref}`, { headers: h });
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`gh contents ${repo}/${path}: ${r.status}`);
  const d = await r.json();
  return { sha: d.sha, text: atob(d.content.replace(/\n/g, "")) };
}

async function ghEnsureBranch(repo, name, fromBranch, h) {
  const headRef = await fetch(`https://api.github.com/repos/${repo}/git/ref/heads/${fromBranch}`, { headers: h }).then(r => r.json());
  await fetch(`https://api.github.com/repos/${repo}/git/refs`, {
    method: "POST",
    headers: { ...h, "Content-Type": "application/json" },
    body: JSON.stringify({ ref: `refs/heads/${name}`, sha: headRef.object.sha }),
  });
}

async function ghPutFile(repo, path, content, branch, message, h) {
  const cur = await ghGetFile(repo, path, branch, h);
  const r = await fetch(`https://api.github.com/repos/${repo}/contents/${encodeURIComponent(path)}`, {
    method: "PUT",
    headers: { ...h, "Content-Type": "application/json" },
    body: JSON.stringify({
      message, branch, content: btoa(content),
      sha: cur?.sha,
    }),
  });
  if (!r.ok) throw new Error(`gh put ${repo}/${path}: ${r.status} ${await r.text()}`);
  return r.json();
}

async function ghOpenPr(repo, head, base, body, h) {
  const r = await fetch(`https://api.github.com/repos/${repo}/pulls`, {
    method: "POST",
    headers: { ...h, "Content-Type": "application/json" },
    body: JSON.stringify({
      title: `chore(deps): weekly bump ${weekStamp()}`,
      head, base, body, maintainer_can_modify: true,
    }),
  });
  if (!r.ok) throw new Error(`gh pr ${repo}: ${r.status} ${await r.text()}`);
  return r.json();
}

function prBody(updates) {
  const lines = ["## Dependency updates", ""];
  const groups = { pypi: [], npm: [] };
  for (const u of updates) groups[u.ecosystem].push(u);
  if (groups.pypi.length) {
    lines.push("### PyPI"); lines.push("| package | current | latest |"); lines.push("|---|---|---|");
    for (const u of groups.pypi) lines.push(`| \`${u.pkg}\` | ${u.current} | ${u.latest} |`);
    lines.push("");
  }
  if (groups.npm.length) {
    lines.push("### npm"); lines.push("| package | current | latest |"); lines.push("|---|---|---|");
    for (const u of groups.npm) lines.push(`| \`${u.pkg}\` | ${u.current} | ${u.latest} |`);
    lines.push("");
  }
  lines.push("---");
  lines.push("Opened by `dep-update-worker` (CF Cron). Merge after CI green.");
  lines.push(`Run id: ${weekStamp()}`);
  return lines.join("\n");
}

function weekStamp() {
  const d = new Date();
  const onejan = new Date(d.getFullYear(), 0, 1);
  const week = Math.ceil((((d - onejan) / 86400000) + onejan.getDay() + 1) / 7);
  return `${d.getFullYear()}w${String(week).padStart(2, "0")}`;
}
