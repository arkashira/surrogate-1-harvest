#!/usr/bin/env bash
# Codebase indexer — scans axentx + AI-Hub projects, NEVER ~/develope.
# Writes one markdown per project to AI-Hub/knowledge/projects/.
# Metadata only (no code content): language, deps, scripts, README, recent commits, structure.
#
# PRIVACY (2026-05-03): default SRC was ~/develope which contains
# private client work (Thinkbit Excise/Wine/Car/MFA/RD, etc). Default
# changed to ~/axentx. To re-include external repos, pass an explicit
# arg AFTER confirming each is OK to ingest. Never pass ~/develope.
set -e

SRC="${1:-$HOME/axentx}"
# Refuse to run against ~/develope even if explicitly passed
case "$(realpath "$SRC" 2>/dev/null || echo "$SRC")" in
    "$HOME/develope"*|"/Users/Ashira/develope"*)
        echo "ERROR: ~/develope/ is private client work, never index it." >&2
        exit 2 ;;
esac
DEST="$HOME/Documents/Obsidian Vault/AI-Hub/knowledge/projects"
export PATH=/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin:$PATH
PY=~/.claude/venv/bin/python

mkdir -p "$DEST"
[ ! -d "$SRC" ] && { echo "No such dir: $SRC"; exit 1; }

SRC="$SRC" DEST="$DEST" "$PY" <<'PY'
import os, json, re, subprocess
from pathlib import Path
from datetime import datetime, date

src = Path(os.environ['SRC']).expanduser()
dest = Path(os.environ['DEST'])
dest.mkdir(parents=True, exist_ok=True)

SKIP_DIRS = {'node_modules', '.git', '.next', 'dist', 'build', '__pycache__', '.venv', 'venv',
             'target', 'bin', 'obj', '.idea', '.vscode', '.DS_Store', 'coverage',
             '.terraform', '.nuxt', '.parcel-cache', '.pnpm-store'}

def is_project_root(p: Path) -> bool:
    markers = ['package.json', 'go.mod', 'requirements.txt', 'pyproject.toml', 'Cargo.toml',
               'pom.xml', 'build.gradle', 'Gemfile', 'composer.json', 'Makefile',
               'Dockerfile', 'docker-compose.yml', 'docker-compose.yaml',
               'serverless.yml', 'template.yaml', 'template.yml']
    return any((p / m).exists() for m in markers)

def detect_stack(p: Path) -> dict:
    stack = {'languages': [], 'frameworks': [], 'deps_count': 0, 'key_deps': []}

    # package.json (Node)
    pj = p / 'package.json'
    if pj.exists():
        try:
            d = json.loads(pj.read_text())
            stack['languages'].append('JavaScript/TypeScript')
            deps = {**d.get('dependencies',{}), **d.get('devDependencies',{})}
            stack['deps_count'] += len(deps)
            # Top-3 identifiable frameworks
            keymap = {'next':'Next.js','react':'React','vue':'Vue','express':'Express',
                      'fastify':'Fastify','nestjs':'NestJS','@nestjs/core':'NestJS',
                      'typescript':'TypeScript','vite':'Vite','firebase':'Firebase',
                      '@aws-sdk/client-s3':'AWS SDK','prisma':'Prisma','sequelize':'Sequelize',
                      'mssql':'MSSQL','mongoose':'MongoDB'}
            for k, name in keymap.items():
                if k in deps: stack['frameworks'].append(name)
            stack['key_deps'] = sorted(deps.keys())[:15]
            stack['scripts'] = list(d.get('scripts',{}).keys())[:10]
        except: pass

    # go.mod
    gm = p / 'go.mod'
    if gm.exists():
        stack['languages'].append('Go')
        try:
            content = gm.read_text()
            m = re.search(r'^go\s+([\d.]+)', content, re.M)
            if m: stack['go_version'] = m.group(1)
            reqs = re.findall(r'^\s*([\w./-]+)\s+v[\d.]+', content, re.M)
            stack['deps_count'] += len(reqs)
            stack['key_deps'] = reqs[:15]
        except: pass

    # Python
    for pyfile in ['requirements.txt','pyproject.toml']:
        if (p / pyfile).exists():
            stack['languages'].append('Python')
            break

    # Cargo
    if (p / 'Cargo.toml').exists():
        stack['languages'].append('Rust')

    # Docker
    if (p / 'Dockerfile').exists() or (p / 'docker-compose.yml').exists():
        stack['frameworks'].append('Docker')

    # Terraform
    if any(p.glob('*.tf')): stack['frameworks'].append('Terraform')

    # CloudFormation
    for fn in ['template.yaml','template.yml']:
        if (p / fn).exists(): stack['frameworks'].append('CloudFormation'); break

    return stack

def get_readme(p: Path) -> str:
    for name in ['README.md','readme.md','README.rst','README.txt','README']:
        rm = p / name
        if rm.exists():
            try:
                txt = rm.read_text(errors='ignore')
                # First meaningful paragraph (skip badges/frontmatter)
                lines = []
                for line in txt.split('\n')[:80]:
                    s = line.strip()
                    if not s or s.startswith(('[![','#','---','<!--','http')): continue
                    lines.append(s)
                    if sum(len(l) for l in lines) > 400: break
                return ' '.join(lines)[:500]
            except: return ''
    return ''

def git_info(p: Path) -> dict:
    info = {}
    try:
        info['branch'] = subprocess.check_output(
            ['git','-C',str(p),'rev-parse','--abbrev-ref','HEAD'],
            stderr=subprocess.DEVNULL, timeout=5).decode().strip()
        info['last_commit'] = subprocess.check_output(
            ['git','-C',str(p),'log','-1','--format=%ci %s'],
            stderr=subprocess.DEVNULL, timeout=5).decode().strip()[:200]
        info['commits_last_30d'] = subprocess.check_output(
            ['git','-C',str(p),'log','--since=30.days.ago','--oneline'],
            stderr=subprocess.DEVNULL, timeout=5).decode().count('\n')
    except: pass
    return info

def tree_summary(p: Path, max_depth=2) -> list:
    lines = []
    def walk(path, depth=0):
        if depth > max_depth: return
        try:
            items = sorted([x for x in path.iterdir() if x.name not in SKIP_DIRS and not x.name.startswith('.')])
        except: return
        for item in items[:20]:
            lines.append('  '*depth + ('📁 ' if item.is_dir() else '📄 ') + item.name)
            if item.is_dir():
                walk(item, depth+1)
    walk(p)
    return lines[:60]

def loc_estimate(p: Path) -> int:
    total = 0
    for ext in ['*.ts','*.tsx','*.js','*.jsx','*.py','*.go','*.rs','*.java','*.kt','*.swift']:
        for f in p.rglob(ext):
            if any(sk in f.parts for sk in SKIP_DIRS): continue
            try:
                with open(f, errors='ignore') as fp:
                    total += sum(1 for _ in fp)
            except: pass
            if total > 500_000: return total  # stop counting huge ones
    return total

projects = []
# Discover projects — any dir containing a marker file
def find_projects(root, depth=0, max_depth=4):
    if depth > max_depth: return
    try:
        for item in root.iterdir():
            if not item.is_dir() or item.name in SKIP_DIRS or item.name.startswith('.'):
                continue
            if is_project_root(item):
                projects.append(item)
                # Don't recurse into projects (they often contain sub-projects, but we catch monorepos differently)
            else:
                find_projects(item, depth+1, max_depth)
    except: pass

find_projects(src)
print(f'Found {len(projects)} projects under {src}')

written = 0
for p in projects:
    rel = p.relative_to(src.parent if src.parent else src)
    slug = str(rel).replace('/','__').replace(' ','_')

    stack = detect_stack(p)
    readme = get_readme(p)
    git = git_info(p)
    tree = tree_summary(p)
    loc = loc_estimate(p)

    tags = ['project','codebase']
    for l in stack.get('languages',[]):
        tags.append(l.lower().replace('/','-').replace(' ','-'))
    for f in stack.get('frameworks',[])[:5]:
        tags.append(f.lower().replace('/','-').replace(' ','-'))

    body = f"""---
name: {p.name}
path: {p}
tags: {json.dumps(tags)}
last_indexed: {date.today().isoformat()}
type: project
---

# {p.name}

**Path**: `{p}`
**Group**: {p.parent.name if p.parent != src else 'root'}
**Languages**: {', '.join(stack.get('languages',[])) or 'unknown'}
**Frameworks**: {', '.join(stack.get('frameworks',[])) or 'none detected'}
**LOC**: ~{loc:,}
**Deps**: {stack.get('deps_count',0)}

## README
{readme or '(no README found)'}

## Git
- Branch: `{git.get('branch','?')}`
- Last commit: {git.get('last_commit','?')}
- Commits (last 30d): {git.get('commits_last_30d','?')}

## Key dependencies
{chr(10).join(f'- `{d}`' for d in stack.get('key_deps',[])[:15]) or '(none)'}

## Scripts
{chr(10).join(f'- `{s}`' for s in stack.get('scripts',[])) or '(none)'}

## Structure
```
{chr(10).join(tree)}
```

## Related
- [[../../patterns/MOC|Knowledge Graph Hub]]
- [[../workspace-map|Workspace Map]]
"""
    out_file = dest / f'{slug}.md'
    out_file.write_text(body)
    written += 1

# Index file
index = ['---','name: Projects Index','tags: [projects, index]','---','',
         f'# Projects Indexed ({written})','',f'_Last scan: {date.today()}_','']

# Group by parent dir
by_group = {}
for p in projects:
    g = p.parent.name if p.parent != src else 'root'
    by_group.setdefault(g, []).append(p)

for g in sorted(by_group.keys()):
    index.append(f'## {g}')
    for p in sorted(by_group[g], key=lambda x:x.name):
        slug = str(p.relative_to(src.parent if src.parent else src)).replace('/','__').replace(' ','_')
        index.append(f'- [[{slug}|{p.name}]]')
    index.append('')

(dest / 'README.md').write_text('\n'.join(index))
print(f'Wrote {written} project metadata files to {dest}')
PY

# Trigger graph sync
[ -x "/opt/surrogate-1-harvest/bin/graph-sync.sh" ] && ("/opt/surrogate-1-harvest/bin/graph-sync.sh" > /dev/null 2>&1 &) || true
