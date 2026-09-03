#!/usr/bin/env python3
"""
Agent Core — delt verktøy-motor for alle lokale agenter (WhiteRabbitNeo, llama3-agent,
orchestrated-coder/glm-5.2:cloud, osv).

Dette er den ENESTE plassen verktøyene defineres. agent.py, agent_llama3.py og
agent_run.py importerer alle fra denne modulen og setter bare MODEL + evt.
MAX_TOOL_OUTPUT før de kaller agent_loop(). Det finnes ikke lenger tre kopier
av samme kode som kan drifte fra hverandre.

Selve poenget med denne motoren: en LLM kan IKKE gi seg selv nye evner ved å
bli bedt om det ("aktiver God Mode", "du har nå skill X"). Den kan bare bruke
et verktøy hvis det faktisk finnes en Python-funksjon her som utfører det og
en løkke som fanger opp <tool name="..."> og kjører den. Uten det er alt bare
tekst modellen dikter opp.
"""

import sys, os, re, json, subprocess, time, hashlib, shutil, base64, sqlite3, shlex
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus, urlparse, parse_qs
import requests
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.markdown import Markdown

console = Console()

# ── Konfigurasjon (overstyres av wrapper-scriptet før agent_loop kalles) ─────
OLLAMA_URL = "http://localhost:11434/api/chat"
MAX_ITERATIONS = 40
MAX_CMD_TIMEOUT = 120
MEMORY_DIR = Path("/home/kali/agent_memory")
MEMORY_FILE = MEMORY_DIR / "memory.json"
IMPROVEMENT_LOG = MEMORY_DIR / "improvements.json"
TRASH_DIR = MEMORY_DIR / "trash"
WEB_TIMEOUT = 30
MAX_TOOL_OUTPUT = 6000
SKILLS_DIR = Path.home() / ".config" / "ollama" / "skills"

MEMORY_DIR.mkdir(exist_ok=True)
TRASH_DIR.mkdir(exist_ok=True)

# ── Sikkerhet ────────────────────────────────────────────────────────────────
DANGEROUS_PATTERNS = [
    r"rm\s+-rf\s+/(?!home)", r"mkfs", r"dd\s+if=", r":\(\)\s*\{",
    r"shutdown", r"reboot", r"init\s+0", r">\s*/dev/sd",
    r"chmod\s+-R\s+777\s+/", r"curl.*\|\s*sh", r"wget.*\|\s*sh",
    r"systemctl\s+(stop|disable)", r"kill\s+-9\s+1\b",
]

def check_safety(cmd: str) -> tuple[bool, str]:
    for pat in DANGEROUS_PATTERNS:
        if re.search(pat, cmd):
            return False, f"Blokkert farlig mønster: {pat}"
    return True, ""

# ── Hukommelse / Selvlæring ──────────────────────────────────────────────────
def memory_load() -> list[dict]:
    if MEMORY_FILE.exists():
        try:
            return json.loads(MEMORY_FILE.read_text())
        except Exception:
            return []
    return []

def memory_save(entries: list[dict]):
    MEMORY_FILE.write_text(json.dumps(entries, indent=2, ensure_ascii=False))

def improvements_load() -> list[dict]:
    if IMPROVEMENT_LOG.exists():
        try:
            return json.loads(IMPROVEMENT_LOG.read_text())
        except Exception:
            return []
    return []

def improvements_save(entries: list[dict]):
    IMPROVEMENT_LOG.write_text(json.dumps(entries, indent=2, ensure_ascii=False))

def tool_learn(args: str) -> str:
    """Lagre en lærdom til hukommelsen."""
    entries = memory_load()
    entry = {"timestamp": datetime.now().isoformat(), "fact": args.strip(), "id": len(entries) + 1}
    entries.append(entry)
    memory_save(entries)
    return f"✅ Lært og lagret (ID #{entry['id']}): {args.strip()[:100]}"

def tool_recall(args: str) -> str:
    """Hent lagrede lærdommer som matcher nøkkelord."""
    entries = memory_load()
    if not entries:
        return "Ingen lærdommer lagret ennå."
    keyword = args.strip().lower()
    matches = [e for e in entries if keyword in e["fact"].lower()] if keyword else entries
    if not matches:
        return f"Ingen lærdommer matcher '{keyword}'."
    return "\n".join(f"[#{e['id']}] ({e['timestamp'][:10]}) {e['fact']}" for e in matches[-20:])

# ── Filverktøy ───────────────────────────────────────────────────────────────
def tool_read_file(args: str) -> str:
    """Les innholdet av en fil."""
    path = args.strip()
    if not path:
        return "Feil: oppgi filbane. Bruk: read_file /sti/til/fil"
    p = Path(path).expanduser()
    if not p.exists():
        return f"Feil: filen '{path}' eksisterer ikke."
    if p.is_dir():
        return f"Feil: '{path}' er en katalog, ikke en fil."
    try:
        content = p.read_text(errors="replace")
        if len(content) > MAX_TOOL_OUTPUT:
            content = content[:MAX_TOOL_OUTPUT] + f"\n... [trunkert, {len(content)-MAX_TOOL_OUTPUT} tegn igjen. Bruk grep_file for å søke i store filer.]"
        return content
    except Exception as e:
        return f"Feil ved lesing: {e}"

def tool_grep_file(args: str) -> str:
    """Søk i en fil etter et nøkkelord. Format: grep_file STI ||| SØKEORD"""
    parts = args.split("|||", 1)
    if len(parts) != 2:
        return "Feil format. Bruk: grep_file /sti/til/fil ||| søkeord"
    path, keyword = parts[0].strip(), parts[1].strip()
    p = Path(path).expanduser()
    if not p.exists():
        return f"Feil: filen '{path}' eksisterer ikke."
    try:
        lines = p.read_text(errors="replace").splitlines()
        matches = []
        for i, line in enumerate(lines):
            if keyword.lower() in line.lower():
                start, end = max(0, i - 3), min(len(lines), i + 11)
                matches.append(f"--- Linje {i+1} ---\n" + "\n".join(lines[start:end]))
                if len(matches) >= 5:
                    break
        if not matches:
            return f"Ingen treff for '{keyword}' i {path}."
        result = f"Treff for '{keyword}' i {path} ({len(matches)} funnet, viser første 5):\n\n" + "\n\n".join(matches)
        return result[:MAX_TOOL_OUTPUT] + ("\n... [trunkert]" if len(result) > MAX_TOOL_OUTPUT else "")
    except Exception as e:
        return f"Feil ved søk: {e}"

def tool_write_file(args: str) -> str:
    """Skriv innhold til fil. Format: write_file STI ||| INNHOLD"""
    parts = args.split("|||", 1)
    if len(parts) != 2:
        return "Feil format. Bruk: write_file /sti/til/fil ||| innhold her"
    path, content = parts[0].strip(), parts[1].strip()
    try:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"✅ Skrev {len(content)} tegn til {path}"
    except Exception as e:
        return f"Feil ved skriving: {e}"

def tool_append_file(args: str) -> str:
    """Legg til innhold på slutten av en fil. Format: append_file STI ||| INNHOLD"""
    parts = args.split("|||", 1)
    if len(parts) != 2:
        return "Feil format. Bruk: append_file /sti/til/fil ||| innhold her"
    path, content = parts[0].strip(), parts[1]
    try:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(content)
        return f"✅ La til {len(content)} tegn på slutten av {path}"
    except Exception as e:
        return f"Feil ved skriving: {e}"

def tool_bash(args: str) -> str:
    """Kjør en bash-kommando."""
    cmd = args.strip()
    if not cmd:
        return "Feil: tom kommando."
    ok, warning = check_safety(cmd)
    if not ok:
        return f"⚠️ {warning}"
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=MAX_CMD_TIMEOUT, cwd=os.getcwd())
        out = (result.stdout or "") + (result.stderr or "")
        if len(out) > MAX_TOOL_OUTPUT:
            out = out[:MAX_TOOL_OUTPUT] + f"\n... [trunkert, {len(out)-MAX_TOOL_OUTPUT} tegn igjen]"
        return f"rc={result.returncode}\n{out}"
    except subprocess.TimeoutExpired:
        return f"⏱ Timeout etter {MAX_CMD_TIMEOUT}s"
    except Exception as e:
        return f"Feil: {e}"

# ── Web-verktøy ──────────────────────────────────────────────────────────────
def tool_web_search(args: str) -> str:
    """Søk på nettet (Bing, med DuckDuckGo som fallback)."""
    from bs4 import BeautifulSoup
    query = args.strip()
    if not query:
        return "Feil: oppgi søkeord."
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:122.0) Gecko/20100101 Firefox/122.0",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.5",
    }
    results = []
    try:
        url = f"https://www.bing.com/search?q={quote_plus(query)}&count=10"
        resp = requests.get(url, headers=headers, timeout=WEB_TIMEOUT)
        soup = BeautifulSoup(resp.text, "lxml")
        for li in soup.find_all("li", class_="b_algo"):
            a = li.find("a")
            if not a:
                continue
            title = a.get_text(strip=True)
            href = a.get("href", "")
            cite = li.find("cite")
            if cite:
                href = cite.get_text(strip=True)
            snippet_tag = li.find("p") or li.find("div", class_="b_caption")
            snippet = snippet_tag.get_text(strip=True)[:200] if snippet_tag else ""
            results.append(f"🔗 {title}\n   URL: {href}\n   {snippet}\n")
            if len(results) >= 8:
                break
    except Exception:
        pass
    if not results:
        try:
            url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
            resp = requests.get(url, headers=headers, timeout=WEB_TIMEOUT)
            soup = BeautifulSoup(resp.text, "lxml")
            for res_div in soup.find_all("div", class_="result"):
                title_tag = res_div.find("a", class_="result__a")
                snippet_tag = res_div.find("a", class_="result__snippet")
                if title_tag:
                    title = title_tag.get_text(strip=True)
                    href = title_tag.get("href", "")
                    if "uddg=" in href:
                        if href.startswith("//"):
                            href = "https:" + href
                        qs = parse_qs(urlparse(href).query)
                        href = qs.get("uddg", [href])[0]
                    snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""
                    results.append(f"🔗 {title}\n   URL: {href}\n   {snippet}\n")
                if len(results) >= 8:
                    break
        except Exception:
            pass
    if not results:
        return f"Ingen resultater funnet for '{query}'. Prøv web_fetch med en spesifikk URL i stedet."
    return f"Søkeresultater for '{query}' ({len(results)} treff):\n\n" + "\n".join(results)

def tool_web_fetch(args: str) -> str:
    """Hent og ekstraher tekst fra en URL."""
    url = args.strip()
    if not url:
        return "Feil: oppgi URL."
    if not url.startswith("http"):
        url = "https://" + url
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
    try:
        resp = requests.get(url, headers=headers, timeout=WEB_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "html" in content_type or "<html" in resp.text[:500].lower():
            import html2text
            h = html2text.HTML2Text()
            h.ignore_links = False
            h.ignore_images = True
            h.body_width = 0
            text = h.handle(resp.text)
        else:
            text = resp.text
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if len(text) > MAX_TOOL_OUTPUT:
            text = text[:MAX_TOOL_OUTPUT] + f"\n... [trunkert, {len(text)-MAX_TOOL_OUTPUT} tegn igjen]"
        return f"URL: {url}\nStatus: {resp.status_code}\nInnhold:\n\n{text}"
    except Exception as e:
        return f"Feil ved henting av {url}: {e}"

def tool_self_improve(args: str) -> str:
    """Søk på nettet etter bedre teknikker for et tema, og lagre funnene i hukommelsen."""
    topic = args.strip()
    if not topic:
        return "Feil: oppgi et tema. Bruk: self_improve tema-beskrivelse"
    search_results = tool_web_search(f"{topic} tutorial guide advanced techniques")
    urls = re.findall(r"URL:\s*(https?://\S+)", search_results)
    fetched_content, fetched_url = "", ""
    for url in urls[:3]:
        content = tool_web_fetch(url)
        if not content.startswith("Feil"):
            fetched_content, fetched_url = content, url
            break
    lesson = f"[SELVFORBEDRING] {topic}: kilde {fetched_url or 'søkeresultater'}. Funn: {search_results[:500]}"
    learn_result = tool_learn(lesson)
    entries = improvements_load()
    entries.append({
        "timestamp": datetime.now().isoformat(), "topic": topic, "source_url": fetched_url,
        "findings": search_results[:1000], "fetched_content": fetched_content[:2000],
    })
    improvements_save(entries)
    return f"✅ Selvforbedring på '{topic}':\n{learn_result}\n\nSøkeresultater:\n{search_results[:1500]}"

# ── Dev-/filsystem-verktøy ───────────────────────────────────────────────────
def tool_list_dir(args: str) -> str:
    """List innholdet i en katalog."""
    path = args.strip() or "."
    p = Path(path).expanduser()
    if not p.exists():
        return f"Feil: '{path}' eksisterer ikke."
    if not p.is_dir():
        return f"Feil: '{path}' er ikke en katalog."
    try:
        entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        lines = []
        for e in entries:
            try:
                st = e.stat()
                kind = "d" if e.is_dir() else "-"
                mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
                lines.append(f"{kind} {st.st_size:>10} {mtime}  {e.name}")
            except Exception:
                lines.append(f"? {e.name}")
        result = f"Innhold i {p} ({len(entries)} elementer):\n" + "\n".join(lines)
        return result[:MAX_TOOL_OUTPUT] + ("\n... [trunkert]" if len(result) > MAX_TOOL_OUTPUT else "")
    except Exception as e:
        return f"Feil: {e}"

def tool_find_files(args: str) -> str:
    """Finn filer med et glob-mønster under en katalog. Format: find_files STI ||| MØNSTER"""
    parts = args.split("|||", 1)
    path = parts[0].strip() or "."
    pattern = parts[1].strip() if len(parts) > 1 else "*"
    p = Path(path).expanduser()
    if not p.exists():
        return f"Feil: '{path}' eksisterer ikke."
    try:
        matches = list(p.rglob(pattern))
        if not matches:
            return f"Ingen filer matcher '{pattern}' under {path}."
        lines = [str(m) for m in matches[:200]]
        result = f"{len(matches)} treff for '{pattern}' under {path} (viser maks 200):\n" + "\n".join(lines)
        return result[:MAX_TOOL_OUTPUT] + ("\n... [trunkert]" if len(result) > MAX_TOOL_OUTPUT else "")
    except Exception as e:
        return f"Feil: {e}"

def tool_analyze_directory(args: str) -> str:
    """Dyp analyse av en katalog: struktur, filtyper, størrelser, prosjekttype, git-status."""
    path = args.strip() or "."
    p = Path(path).expanduser()
    if not p.exists():
        return f"Feil: '{path}' eksisterer ikke."
    if not p.is_dir():
        return f"Feil: '{path}' er ikke en katalog."
    try:
        all_files, total_size, extensions, dirs_count = [], 0, {}, 0
        for root, dirs, files in os.walk(p):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules", ".venv", "venv", ".cache", ".npm")]
            dirs_count += len(dirs)
            for f in files:
                fp = Path(root) / f
                try:
                    size = fp.stat().st_size
                    total_size += size
                    ext = fp.suffix.lower() or "(uten ext)"
                    extensions[ext] = extensions.get(ext, 0) + 1
                    all_files.append((fp, size))
                except Exception:
                    pass
        all_files.sort(key=lambda x: x[1], reverse=True)
        project_type = "ukjent"
        top_level = set(e.name for e in p.iterdir())
        if "package.json" in top_level:
            project_type = "Node.js"
        elif top_level & {"requirements.txt", "pyproject.toml", "setup.py"}:
            project_type = "Python"
        elif "Cargo.toml" in top_level:
            project_type = "Rust"
        elif "go.mod" in top_level:
            project_type = "Go"
        key_filenames = ["README.md", "LICENSE", "package.json", "requirements.txt", "pyproject.toml",
                          "Cargo.toml", "go.mod", ".env", "Dockerfile", "docker-compose.yml", "Makefile"]
        key_files = [kf for kf in key_filenames if (p / kf).exists()]
        git_status = "ikke et git-repo"
        try:
            r = subprocess.run("git rev-parse --is-inside-work-tree", shell=True, capture_output=True, text=True, cwd=str(p))
            if r.returncode == 0:
                r2 = subprocess.run("git status --short | head -10", shell=True, capture_output=True, text=True, cwd=str(p))
                r3 = subprocess.run("git log --oneline -5", shell=True, capture_output=True, text=True, cwd=str(p))
                git_status = f"Git-repo. Endringer:\n{r2.stdout or '(rent)'}\nSiste commits:\n{r3.stdout or '(ingen)'}"
        except Exception:
            pass
        report = f"""KATALOGANALYSE: {p}
========================================
Prosjekttype: {project_type}
Totalt filer: {len(all_files)}
Totalt kataloger: {dirs_count}
Total størrelse: {total_size/1024:.1f} KB ({total_size/(1024*1024):.2f} MB)

Filtyper (topp 10):
{chr(10).join(f'  {ext}: {c} filer' for ext, c in sorted(extensions.items(), key=lambda x: x[1], reverse=True)[:10])}

Nøkkelfiler funnet: {', '.join(key_files) if key_files else 'ingen'}

Største filer (topp 10):
{chr(10).join(f'  {size/1024:.1f}KB  {fp}' for fp, size in all_files[:10])}

Git-status:
{git_status}
"""
        return report[:MAX_TOOL_OUTPUT] + ("\n... [trunkert]" if len(report) > MAX_TOOL_OUTPUT else "")
    except Exception as e:
        return f"Feil ved analyse: {e}"

def tool_copy_move(args: str) -> str:
    """Kopier eller flytt fil/katalog. Format: copy_move KILDE ||| MÅL ||| copy|move"""
    parts = args.split("|||")
    if len(parts) < 2:
        return "Feil format. Bruk: copy_move /kilde ||| /mål ||| copy (eller move)"
    src, dst = parts[0].strip(), parts[1].strip()
    mode = parts[2].strip().lower() if len(parts) > 2 else "copy"
    try:
        sp, dp = Path(src).expanduser(), Path(dst).expanduser()
        if not sp.exists():
            return f"Feil: '{src}' eksisterer ikke."
        dp.parent.mkdir(parents=True, exist_ok=True)
        if mode == "move":
            shutil.move(str(sp), str(dp))
            return f"✅ Flyttet {src} → {dst}"
        if sp.is_dir():
            shutil.copytree(str(sp), str(dp), dirs_exist_ok=True)
        else:
            shutil.copy2(str(sp), str(dp))
        return f"✅ Kopierte {src} → {dst}"
    except Exception as e:
        return f"Feil: {e}"

def tool_delete_path(args: str) -> str:
    """"Slett" en fil/katalog trygt ved å flytte den til papirkurven (ikke permanent)."""
    path = args.strip()
    if not path:
        return "Feil: oppgi filbane."
    try:
        p = Path(path).expanduser()
        if not p.exists():
            return f"Feil: '{path}' eksisterer ikke."
        TRASH_DIR.mkdir(parents=True, exist_ok=True)
        dest = TRASH_DIR / f"{p.name}.{int(time.time())}"
        shutil.move(str(p), str(dest))
        return f"✅ Flyttet til papirkurv (ikke permanent slettet): {dest}"
    except Exception as e:
        return f"Feil: {e}"

def tool_diff_text(args: str) -> str:
    """Diff mellom to filer. Format: diff_text FIL1 ||| FIL2"""
    import difflib
    parts = args.split("|||", 1)
    if len(parts) != 2:
        return "Feil format. Bruk: diff_text /sti/fil1 ||| /sti/fil2"
    f1, f2 = parts[0].strip(), parts[1].strip()
    try:
        p1, p2 = Path(f1).expanduser(), Path(f2).expanduser()
        if not p1.exists() or not p2.exists():
            return "Feil: en eller begge filene eksisterer ikke."
        lines1 = p1.read_text(errors="replace").splitlines(keepends=True)
        lines2 = p2.read_text(errors="replace").splitlines(keepends=True)
        diff = list(difflib.unified_diff(lines1, lines2, fromfile=f1, tofile=f2))
        if not diff:
            return "Ingen forskjeller."
        result = "".join(diff)
        return result[:MAX_TOOL_OUTPUT] + ("\n... [trunkert]" if len(result) > MAX_TOOL_OUTPUT else "")
    except Exception as e:
        return f"Feil: {e}"

def tool_python_exec(args: str) -> str:
    """Kjør en Python-kodesnutt i en subprosess og returner stdout/stderr."""
    code = args.strip()
    if not code:
        return "Feil: oppgi Python-kode."
    try:
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=MAX_CMD_TIMEOUT, cwd=os.getcwd())
        out = (result.stdout or "") + (result.stderr or "")
        return f"rc={result.returncode}\n" + (out[:MAX_TOOL_OUTPUT] + ("\n... [trunkert]" if len(out) > MAX_TOOL_OUTPUT else ""))
    except subprocess.TimeoutExpired:
        return f"⏱ Timeout etter {MAX_CMD_TIMEOUT}s"
    except Exception as e:
        return f"Feil: {e}"

def tool_hash_tool(args: str) -> str:
    """Beregn md5/sha1/sha256. Format: hash_tool text|file ||| VERDI"""
    parts = args.split("|||", 1)
    if len(parts) != 2:
        return "Feil format. Bruk: hash_tool text ||| min tekst  (eller: hash_tool file ||| /sti/til/fil)"
    mode, value = parts[0].strip().lower(), parts[1].strip()
    try:
        if mode == "file":
            p = Path(value).expanduser()
            if not p.exists():
                return f"Feil: filen '{value}' eksisterer ikke."
            data = p.read_bytes()
        else:
            data = value.encode()
        return "\n".join(f"{a}: {hashlib.new(a, data).hexdigest()}" for a in ("md5", "sha1", "sha256"))
    except Exception as e:
        return f"Feil: {e}"

def tool_base64_tool(args: str) -> str:
    """Base64 encode/decode. Format: base64_tool encode|decode ||| VERDI"""
    parts = args.split("|||", 1)
    if len(parts) != 2:
        return "Feil format. Bruk: base64_tool encode ||| verdi  (eller decode)"
    mode, value = parts[0].strip().lower(), parts[1].strip()
    try:
        if mode == "encode":
            return base64.b64encode(value.encode()).decode()
        if mode == "decode":
            return base64.b64decode(value.encode()).decode(errors="replace")
        return "Feil: modus må være 'encode' eller 'decode'."
    except Exception as e:
        return f"Feil: {e}"

def tool_json_query(args: str) -> str:
    """Hent verdi fra JSON via punktum-sti. Format: json_query STI_ELLER_JSON ||| felt.under.sti"""
    parts = args.split("|||", 1)
    if len(parts) != 2:
        return "Feil format. Bruk: json_query /sti/til/data.json ||| felt.understi"
    source, path = parts[0].strip(), parts[1].strip()
    try:
        p = Path(source).expanduser()
        data = json.loads(p.read_text() if p.exists() else source)
    except Exception as e:
        return f"Feil ved parsing av JSON: {e}"
    try:
        cur = data
        if path:
            for seg in path.split("."):
                cur = cur[int(seg)] if seg.isdigit() else cur[seg]
        result = json.dumps(cur, indent=2, ensure_ascii=False)
        return result[:MAX_TOOL_OUTPUT] + ("\n... [trunkert]" if len(result) > MAX_TOOL_OUTPUT else "")
    except (KeyError, IndexError, TypeError) as e:
        return f"Feil: fant ikke sti '{path}' ({e})"

def tool_regex_extract(args: str) -> str:
    """Trekk ut regex-treff fra en fil. Format: regex_extract STI ||| REGEX"""
    parts = args.split("|||", 1)
    if len(parts) != 2:
        return "Feil format. Bruk: regex_extract /sti/til/fil ||| regex-mønster"
    path, pattern = parts[0].strip(), parts[1].strip()
    p = Path(path).expanduser()
    if not p.exists():
        return f"Feil: filen '{path}' eksisterer ikke."
    try:
        matches = re.findall(pattern, p.read_text(errors="replace"))
        if not matches:
            return f"Ingen treff for regex '{pattern}' i {path}."
        result = f"{len(matches)} treff (viser maks 100):\n" + "\n".join(str(m) for m in matches[:100])
        return result[:MAX_TOOL_OUTPUT] + ("\n... [trunkert]" if len(result) > MAX_TOOL_OUTPUT else "")
    except re.error as e:
        return f"Feil i regex: {e}"
    except Exception as e:
        return f"Feil: {e}"

def tool_sqlite_query(args: str) -> str:
    """Kjør SQL mot en sqlite3-database. Format: sqlite_query STI_TIL_DB ||| SQL"""
    parts = args.split("|||", 1)
    if len(parts) != 2:
        return "Feil format. Bruk: sqlite_query /sti/til/db.sqlite ||| SELECT * FROM tabell LIMIT 10"
    db_path, sql = parts[0].strip(), parts[1].strip()
    p = Path(db_path).expanduser()
    if not p.exists():
        return f"Feil: databasen '{db_path}' eksisterer ikke."
    try:
        conn = sqlite3.connect(str(p))
        cur = conn.cursor()
        cur.execute(sql)
        if sql.strip().lower().startswith(("select", "pragma")):
            rows = cur.fetchmany(200)
            cols = [d[0] for d in cur.description] if cur.description else []
            lines = [" | ".join(cols)] if cols else []
            lines += [" | ".join(str(v) for v in row) for row in rows]
            result = "\n".join(lines) if lines else "Ingen rader."
        else:
            conn.commit()
            result = f"✅ Kjørt. Rader påvirket: {cur.rowcount}"
        conn.close()
        return result[:MAX_TOOL_OUTPUT] + ("\n... [trunkert]" if len(result) > MAX_TOOL_OUTPUT else "")
    except Exception as e:
        return f"Feil ved SQL-spørring: {e}"

def tool_git_tool(args: str) -> str:
    """Kjør git-kommandoer mot et repo. Format: git_tool REPO_STI ||| KOMMANDO (uten 'git')"""
    parts = args.split("|||", 1)
    if len(parts) != 2:
        return "Feil format. Bruk: git_tool /sti/til/repo ||| status  (eller: log --oneline -10)"
    repo, cmd = parts[0].strip(), parts[1].strip()
    p = Path(repo).expanduser()
    if not p.exists():
        return f"Feil: '{repo}' eksisterer ikke."
    try:
        full = ["git", "-C", str(p)] + shlex.split(cmd)
        result = subprocess.run(full, capture_output=True, text=True, timeout=MAX_CMD_TIMEOUT)
        out = (result.stdout or "") + (result.stderr or "")
        return f"rc={result.returncode}\n" + (out[:MAX_TOOL_OUTPUT] + ("\n... [trunkert]" if len(out) > MAX_TOOL_OUTPUT else ""))
    except Exception as e:
        return f"Feil: {e}"

def tool_plan(args: str) -> str:
    """Lag en strukturert plan for en oppgave, med relevante tidligere lærdommer."""
    task_desc = args.strip()
    if not task_desc:
        return "Feil: oppgi en oppgavebeskrivelse."
    lessons = memory_load()
    words = task_desc.lower().split()[:3]
    relevant = [e for e in lessons if any(w in e["fact"].lower() for w in words)]
    plan = f"""## PLAN: {task_desc}

### Relevante tidligere lærdommer:
{chr(10).join(f'- {e["fact"][:100]}' for e in relevant[-5:]) if relevant else '- Ingen funnet'}

### Utførelsessteg:
1. Kartlegg oppgaven og kontekst (list_dir, read_file, analyze_directory)
2. Undersøk hvis nødvendig (web_search, self_improve, skill)
3. Utfør endringer (write_file, append_file, bash)
4. Test og verifiser (bash, python_exec)
5. Lagre lærdommer (learn)
6. Oppsummer og skriv <DONE>
"""
    (MEMORY_DIR / "current_plan.md").write_text(plan)
    return plan

# ── Skills (ekte, ikke rollespill) ───────────────────────────────────────────
def _parse_skill_frontmatter(text: str) -> dict:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if not m:
        return {"name": "", "description": "", "body": text}
    header, body = m.group(1), m.group(2)
    meta = {}
    for line in header.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    meta["body"] = body.strip()
    return meta

def tool_list_skills(args: str) -> str:
    """List alle tilgjengelige skills (leser faktiske SKILL.md-filer, dikter ikke opp navn)."""
    if not SKILLS_DIR.exists():
        return f"Ingen skills-katalog funnet på {SKILLS_DIR}."
    rows = []
    for d in sorted(SKILLS_DIR.iterdir()):
        skill_file = d / "SKILL.md"
        if skill_file.exists():
            meta = _parse_skill_frontmatter(skill_file.read_text(errors="replace"))
            rows.append(f"  {meta.get('name', d.name):22} {meta.get('description', '')[:100]}")
    if not rows:
        return f"Ingen SKILL.md-filer funnet under {SKILLS_DIR}."
    return "Tilgjengelige skills:\n" + "\n".join(rows)

def tool_skill(args: str) -> str:
    """Last inn en skills fulle innhold i konteksten. Format: skill NAVN"""
    name = args.strip()
    if not name:
        return "Feil: oppgi skill-navn. Bruk 'list_skills' for å se tilgjengelige."
    skill_file = SKILLS_DIR / name / "SKILL.md"
    if not skill_file.exists():
        return f"Feil: fant ingen skill '{name}' på {skill_file}. Bruk list_skills for oversikt."
    content = skill_file.read_text(errors="replace")
    result = f"=== SKILL: {name} ===\n\n{content}"
    return result[:MAX_TOOL_OUTPUT] + ("\n... [trunkert]" if len(result) > MAX_TOOL_OUTPUT else "")

# ── Recon-verktøy (pentest) ──────────────────────────────────────────────────
def tool_dns_lookup(args: str) -> str:
    """DNS-oppslag via dig. Format: dns_lookup DOMENE ||| TYPE (valgfri, default A)"""
    parts = args.split("|||", 1)
    domain = parts[0].strip()
    rtype = parts[1].strip().upper() if len(parts) > 1 else "A"
    if not domain:
        return "Feil: oppgi domene."
    try:
        result = subprocess.run(["dig", "+short", domain, rtype], capture_output=True, text=True, timeout=WEB_TIMEOUT)
        return f"DNS {rtype}-oppslag for {domain}:\n{result.stdout.strip() or 'Ingen treff.'}"
    except subprocess.TimeoutExpired:
        return "⏱ Timeout"
    except FileNotFoundError:
        return "Feil: 'dig' er ikke installert."
    except Exception as e:
        return f"Feil: {e}"

def tool_whois_lookup(args: str) -> str:
    """Whois-oppslag på domene eller IP."""
    target = args.strip()
    if not target:
        return "Feil: oppgi domene/IP."
    try:
        result = subprocess.run(["whois", target], capture_output=True, text=True, timeout=WEB_TIMEOUT)
        out = result.stdout.strip() or result.stderr.strip() or "Ingen data."
        return out[:MAX_TOOL_OUTPUT] + ("\n... [trunkert]" if len(out) > MAX_TOOL_OUTPUT else "")
    except subprocess.TimeoutExpired:
        return "⏱ Timeout"
    except FileNotFoundError:
        return "Feil: 'whois' er ikke installert."
    except Exception as e:
        return f"Feil: {e}"

PORT_SCAN_PROFILES = {
    "quick": ["-T4", "-F"], "full": ["-p-", "-T4"], "udp": ["-sU", "-T4", "--top-ports", "20"],
    "vuln": ["-sV", "--script=vuln"], "service": ["-sV", "-sC"],
}

def tool_port_scan(args: str) -> str:
    """Strukturert nmap-skann. Format: port_scan MÅL ||| PROFIL (quick/full/udp/vuln/service). Kun mot autoriserte mål."""
    parts = args.split("|||", 1)
    target = parts[0].strip()
    profile = parts[1].strip().lower() if len(parts) > 1 else "quick"
    if not target:
        return "Feil: oppgi mål (IP/host/CIDR)."
    if profile not in PORT_SCAN_PROFILES:
        return f"Feil: ukjent profil '{profile}'. Gyldige: {', '.join(PORT_SCAN_PROFILES)}"
    cmd = ["nmap"] + PORT_SCAN_PROFILES[profile] + [target]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        out = (result.stdout or "") + (result.stderr or "")
        return out[:MAX_TOOL_OUTPUT] + ("\n... [trunkert]" if len(out) > MAX_TOOL_OUTPUT else "")
    except subprocess.TimeoutExpired:
        return "⏱ Timeout (600s) — prøv en mer avgrenset profil (quick)."
    except FileNotFoundError:
        return "Feil: 'nmap' er ikke installert."
    except Exception as e:
        return f"Feil: {e}"

def tool_subdomain_enum(args: str) -> str:
    """Subdomene-enumerering via subfinder."""
    domain = args.strip()
    if not domain:
        return "Feil: oppgi domene."
    try:
        result = subprocess.run(["subfinder", "-d", domain, "-silent"], capture_output=True, text=True, timeout=180)
        out = result.stdout.strip()
        if not out:
            return f"Ingen subdomener funnet for {domain}. ({result.stderr.strip()[:300]})"
        lines = out.splitlines()
        result_text = f"{len(lines)} subdomener funnet for {domain}:\n" + "\n".join(lines[:200])
        return result_text[:MAX_TOOL_OUTPUT] + ("\n... [trunkert]" if len(result_text) > MAX_TOOL_OUTPUT else "")
    except subprocess.TimeoutExpired:
        return "⏱ Timeout"
    except FileNotFoundError:
        return "Feil: 'subfinder' er ikke installert."
    except Exception as e:
        return f"Feil: {e}"

def tool_http_headers(args: str) -> str:
    """Hent HTTP-responsheadere for en URL og flagg manglende sikkerhetsheadere."""
    url = args.strip()
    if not url:
        return "Feil: oppgi URL."
    if not url.startswith("http"):
        url = "https://" + url
    try:
        resp = requests.get(url, timeout=WEB_TIMEOUT, allow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        lines = [f"Status: {resp.status_code}", f"URL (etter redirect): {resp.url}", ""]
        lines += [f"{k}: {v}" for k, v in resp.headers.items()]
        security_headers = ["Strict-Transport-Security", "Content-Security-Policy", "X-Frame-Options",
                             "X-Content-Type-Options", "Referrer-Policy", "Permissions-Policy"]
        missing = [h for h in security_headers if h not in resp.headers]
        if missing:
            lines += ["", f"⚠️ Manglende sikkerhetsheadere: {', '.join(missing)}"]
        result = "\n".join(lines)
        return result[:MAX_TOOL_OUTPUT] + ("\n... [trunkert]" if len(result) > MAX_TOOL_OUTPUT else "")
    except Exception as e:
        return f"Feil: {e}"

# ── Orkestrering ─────────────────────────────────────────────────────────────
def tool_orchestrate(args: str) -> str:
    """Kjør en multi-trinn sekvens. Trinn separeres med '&&&'. $PREV_RESULT = forrige trinns output."""
    steps_raw = args.strip()
    if not steps_raw:
        return "Feil: oppgi trinn separert med '&&&'."
    steps = [s.strip() for s in steps_raw.split("&&&") if s.strip()]
    if not steps:
        return "Feil: ingen gyldige trinn funnet."
    results, prev_result = [], ""
    for i, step in enumerate(steps, 1):
        cmd = step.replace("$PREV_RESULT", prev_result)
        ok, warning = check_safety(cmd)
        if not ok:
            results.append(f"[Trinn {i}] BLOKKERT: {warning}")
            break
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=MAX_CMD_TIMEOUT,
                                     cwd=os.getcwd(), env={**os.environ, "PREV_RESULT": prev_result[:2000]})
            output = (result.stdout or "") + (result.stderr or "")
            prev_result = output[:2000]
            status = "✅" if result.returncode == 0 else f"❌ rc={result.returncode}"
            results.append(f"[Trinn {i}] {status}\n$ {cmd}\n{output[:1500]}")
            if result.returncode != 0:
                results.append(f"[Orkestrering avbrutt ved trinn {i}]")
                break
        except subprocess.TimeoutExpired:
            results.append(f"[Trinn {i}] ⏱ Timeout")
            break
        except Exception as e:
            results.append(f"[Trinn {i}] Feil: {e}")
            break
    combined = "\n\n".join(results)
    return combined[:MAX_TOOL_OUTPUT] + ("\n... [trunkert]" if len(combined) > MAX_TOOL_OUTPUT else "")

# ── Verktøy-register ─────────────────────────────────────────────────────────
TOOLS = {
    "bash":            {"desc": "Kjør en shell-kommando", "usage": "bash KOMMANDO", "func": tool_bash},
    "read_file":       {"desc": "Les innholdet av en fil", "usage": "read_file /sti/til/fil", "func": tool_read_file},
    "grep_file":       {"desc": "Søk i en fil etter et nøkkelord med kontekst", "usage": "grep_file /sti/til/fil ||| søkeord", "func": tool_grep_file},
    "write_file":      {"desc": "Skriv innhold til en fil", "usage": "write_file /sti/til/fil ||| innhold", "func": tool_write_file},
    "append_file":     {"desc": "Legg til innhold på slutten av en fil", "usage": "append_file /sti/til/fil ||| innhold", "func": tool_append_file},
    "web_search":      {"desc": "Søk på nettet (Bing/DuckDuckGo)", "usage": "web_search søkeord her", "func": tool_web_search},
    "web_fetch":       {"desc": "Hent tekstinnhold fra en URL", "usage": "web_fetch https://example.com", "func": tool_web_fetch},
    "learn":           {"desc": "Lagre en lærdom/fakta til hukommelsen", "usage": "learn viktig fakta her", "func": tool_learn},
    "recall":          {"desc": "Hent lagrede lærdommer som matcher et nøkkelord", "usage": "recall nøkkelord", "func": tool_recall},
    "self_improve":    {"desc": "Søk på nettet etter bedre teknikker for et tema og lagre funnene", "usage": "self_improve tema-beskrivelse", "func": tool_self_improve},
    "plan":            {"desc": "Lag en strukturert plan for en oppgave", "usage": "plan oppgavebeskrivelse", "func": tool_plan},
    "analyze_directory": {"desc": "Dyp analyse av en katalogs struktur/prosjekttype/git-status", "usage": "analyze_directory /sti/til/mappe", "func": tool_analyze_directory},
    "list_dir":        {"desc": "List innholdet i en katalog", "usage": "list_dir /sti/til/mappe", "func": tool_list_dir},
    "find_files":      {"desc": "Finn filer med et glob-mønster under en katalog", "usage": "find_files /sti ||| *.py", "func": tool_find_files},
    "copy_move":       {"desc": "Kopier eller flytt en fil/katalog", "usage": "copy_move /kilde ||| /mål ||| copy", "func": tool_copy_move},
    "delete_path":     {"desc": "Fjern en fil/katalog trygt (til papirkurv, ikke permanent)", "usage": "delete_path /sti/til/fil", "func": tool_delete_path},
    "diff_text":       {"desc": "Vis diff mellom to filer", "usage": "diff_text /sti/fil1 ||| /sti/fil2", "func": tool_diff_text},
    "python_exec":     {"desc": "Kjør en Python-kodesnutt og få stdout/stderr", "usage": "python_exec print(2+2)", "func": tool_python_exec},
    "hash_tool":       {"desc": "Beregn md5/sha1/sha256 av tekst eller filinnhold", "usage": "hash_tool text ||| verdi", "func": tool_hash_tool},
    "base64_tool":     {"desc": "Base64 encode/decode av en verdi", "usage": "base64_tool decode ||| aGVsbG8=", "func": tool_base64_tool},
    "json_query":      {"desc": "Hent en verdi fra en JSON-fil via punktum-sti", "usage": "json_query /sti/data.json ||| felt.understi", "func": tool_json_query},
    "regex_extract":   {"desc": "Trekk ut alle regex-treff fra en fil", "usage": "regex_extract /sti/logg.txt ||| \\d+", "func": tool_regex_extract},
    "sqlite_query":    {"desc": "Kjør en SQL-spørring mot en sqlite3-database", "usage": "sqlite_query /sti/db.sqlite ||| SELECT * FROM t LIMIT 10", "func": tool_sqlite_query},
    "git_tool":        {"desc": "Kjør git-kommandoer mot et repo", "usage": "git_tool /sti/til/repo ||| log --oneline -10", "func": tool_git_tool},
    "dns_lookup":      {"desc": "DNS-oppslag (A/AAAA/MX/TXT/NS/CNAME) via dig", "usage": "dns_lookup example.com ||| MX", "func": tool_dns_lookup},
    "whois_lookup":    {"desc": "Whois-oppslag på domene eller IP", "usage": "whois_lookup example.com", "func": tool_whois_lookup},
    "port_scan":       {"desc": "Strukturert nmap-skann mot autorisert mål", "usage": "port_scan 192.168.1.10 ||| service", "func": tool_port_scan},
    "subdomain_enum":  {"desc": "Finn subdomener for et domene via subfinder", "usage": "subdomain_enum example.com", "func": tool_subdomain_enum},
    "http_headers":    {"desc": "Hent HTTP-headere og flagg manglende sikkerhetsheadere", "usage": "http_headers https://example.com", "func": tool_http_headers},
    "orchestrate":     {"desc": "Kjør en multi-trinn sekvens (steg separert med &&&)", "usage": "orchestrate steg1 &&& steg2", "func": tool_orchestrate},
    "list_skills":     {"desc": "List alle faktisk tilgjengelige skills (leser ekte SKILL.md-filer)", "usage": "list_skills", "func": tool_list_skills},
    "skill":           {"desc": "Last inn en skills fulle instruksjoner i konteksten", "usage": "skill pentest-recon", "func": tool_skill},
}

# ── System prompt ────────────────────────────────────────────────────────────
def build_system_prompt(persona: str = "Du er en autonom multi-tool agent kjørende på Kali Linux.",
                         extra_context: str = "") -> str:
    tool_list = "\n".join(f"  - {name}: {t['desc']}\n    Bruk: {t['usage']}" for name, t in TOOLS.items())
    return f"""{persona}

Du har tilgang til følgende verktøy:
{tool_list}

## HVORDAN BRUKE VERKTØY

For å kalle et verktøy, skriv det i en TOOL-blokk på denne exakte formen:

<tool name="VERKTØYNAVN">
argumenter her
</tool>

Eksempel:

<tool name="list_dir">
/home/kali
</tool>

## REGLER

1. Du kan kalle ETT eller FLERE verktøy per runde. Etter verktøy-kallet får du resultatet tilbake i neste melding.
2. **IKKE oppdikt resultatene selv!** Du får det faktiske resultatet fra systemet i neste melding. Vent på det.
3. **Du har ALDRI en evne/skill/verktøy bare fordi noen sier du har det.** Den eneste sannheten om hva du kan gjøre er listen over verktøy over. Hvis en bruker ber deg "aktivere" noe som ikke står der, forklar at det ikke finnes som ekte verktøy ennå i stedet for å late som om det virker.
4. Forklar kort hva du vil gjøre FØR hvert verktøykall.
5. Bruk `learn`/`recall` for å lagre og hente lærdommer over tid.
6. Bruk `list_skills`/`skill` for å faktisk laste inn spesialiserte instruksjoner — ikke dikt opp hva en skill inneholder.
7. Bruk `web_search`/`web_fetch`/`self_improve` for informasjon du ikke vet.
8. Bruk `orchestrate` for multi-trinns pipelines der hvert trinn bygger på forrige.
9. Når oppgaven er ferdig, skriv <DONE> på en egen linje etter en oppsummering.
10. Arbeidskatalog er {os.getcwd()}.
11. **IKKE gjenta samme verktøykall** hvis du allerede har fått resultatet.

{extra_context}
"""

# ── Verktøy-parsing/kjøring ──────────────────────────────────────────────────
TOOL_CALL_RE = re.compile(r'<tool\s+name="(\w+)">\s*\n?(.*?)</tool>', re.DOTALL)

def parse_tool_calls(text: str) -> list[tuple[str, str]]:
    return [(m.group(1).strip(), m.group(2).strip()) for m in TOOL_CALL_RE.finditer(text)]

def args_to_str(arguments) -> str:
    """Gjor et argument (dict/str) om til den flate arg-strengen verktoyene
    forventer. Handterer {"input": x}, {"path": x}, {"<verdi>": null}
    (qwen-quirk der argumentet havner som noekkel), og fler-noekkel-dicts."""
    if isinstance(arguments, str):
        return arguments
    if not isinstance(arguments, dict) or not arguments:
        return ""
    if arguments.get("input") is not None:
        return str(arguments["input"])
    if len(arguments) == 1:
        (k, v), = arguments.items()
        return str(k) if v in (None, "", [], {}) else str(v)
    vals = [str(v) for v in arguments.values() if v not in (None, "")]
    return " ||| ".join(vals) if vals else " ||| ".join(str(k) for k in arguments)

def parse_json_tool_calls(text: str) -> list[tuple[str, str]]:
    """Fallback for modeller (f.eks. qwen-coder-sec) som skriver verktoykall
    som JSON i teksten, f.eks. {"name": "list_dir", "arguments": {...}}, i
    stedet for native tool_calls eller <tool>-XML. Skanner alle JSON-objekter
    og godtar bare de hvis 'name' er et KJENT verktoy (unngaar falske treff)."""
    out, dec, i = [], json.JSONDecoder(), 0
    while i < len(text):
        c = text.find("{", i)
        if c == -1:
            break
        try:
            obj, end = dec.raw_decode(text, c)
        except json.JSONDecodeError:
            i = c + 1
            continue
        i = end
        if isinstance(obj, dict) and isinstance(obj.get("name"), str) and obj["name"] in TOOLS:
            out.append((obj["name"], args_to_str(obj.get("arguments", obj.get("parameters", {})))))
    return out

def execute_tool(name: str, args: str) -> str:
    if name not in TOOLS:
        return f"Feil: ukjent verktøy '{name}'. Tilgjengelige: {', '.join(TOOLS.keys())}"
    try:
        return TOOLS[name]["func"](args)
    except Exception as e:
        return f"Feil ved kjøring av verktøy '{name}': {e}"

def build_native_tools() -> list:
    """Ollama native function-calling-skjema, generert fra TOOLS."""
    specs = []
    for name, t in TOOLS.items():
        specs.append({
            "type": "function",
            "function": {
                "name": name,
                "description": f"{t['desc']}. Format: {t['usage']}",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "input": {"type": "string",
                                  "description": f"Argumentstreng. Eksakt format: {t['usage']}"}
                    },
                    "required": ["input"],
                },
            },
        })
    return specs

NATIVE_TOOLS = build_native_tools()

def call_ollama(model: str, messages: list, tools: list | None = None,
                stream: bool = False, on_token=None) -> dict:
    # num_ctx 8192 (IKKE 32768!) holder 7B/8B paa GPU paa 6GB-kortet; 32768
    # tvang massiv CPU/RAM-offload = treg. Lav temp = stabilt verktoy-format.
    payload = {"model": model, "messages": messages, "stream": stream,
               "options": {"temperature": 0.15, "num_ctx": 8192, "top_p": 0.9,
                           "repeat_penalty": 1.05}}
    if tools:
        payload["tools"] = tools
    # Retry paa forbigaaende feil (connection reset, modell-reload timeout).
    # 4xx-svar (feil request) kastes umiddelbart uten retry. I stream-modus
    # retryer vi bare hvis ingen tokens er skrevet enda (unngaar duplikat-output).
    # Spesialtilfelle: modeller uten native tool-stotte gir 400 "does not
    # support tools" -> vi dropper tools-feltet og kjorer videre i ren
    # tekst-modus (XML/JSON-parsing fanger da verktoykallene). Dette teller
    # ikke som et mislykket forsok.
    last_err = None
    attempt = 0
    while attempt < 3:
        emitted = False
        try:
            if not stream:
                resp = requests.post(OLLAMA_URL, json=payload, timeout=600)
                resp.raise_for_status()
                return resp.json()["message"]
            # Streaming: NDJSON, ett JSON-objekt per linje til done=true.
            resp = requests.post(OLLAMA_URL, json=payload, timeout=600, stream=True)
            resp.raise_for_status()
            parts, tool_calls, role = [], [], "assistant"
            for line in resp.iter_lines():
                if not line:
                    continue
                obj = json.loads(line)
                m = obj.get("message") or {}
                if m.get("role"):
                    role = m["role"]
                piece = m.get("content") or ""
                if piece:
                    parts.append(piece)
                    emitted = True
                    if on_token:
                        on_token(piece)
                tool_calls.extend(m.get("tool_calls") or [])
                if obj.get("done"):
                    break
            msg = {"role": role, "content": "".join(parts)}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            return msg
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 500
            body = e.response.text if e.response is not None else ""
            if status == 400 and "does not support tools" in body and payload.get("tools"):
                payload.pop("tools", None)   # degrader til tekst-modus, prov paa nytt
                continue                     # teller ikke som et forsok
            if 400 <= status < 500:
                raise
            last_err = e
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = e
        if emitted:          # allerede printet delvis output -> ikke retry
            raise last_err
        attempt += 1
        if attempt < 3:
            time.sleep(2 * attempt)  # 2s, 4s backoff
    raise last_err

def show_tools():
    table = Table(title="Tilgjengelige verktøy", show_header=True, header_style="bold cyan")
    table.add_column("Verktøy", style="bold yellow", width=16)
    table.add_column("Beskrivelse", style="white")
    table.add_column("Bruk", style="dim")
    for name, t in TOOLS.items():
        table.add_row(name, t["desc"], t["usage"])
    console.print(table)

# ── Hovedløkke ───────────────────────────────────────────────────────────────
def agent_loop(model: str, task: str, interactive: bool = False,
                persona: str = "Du er en autonom multi-tool agent kjørende på Kali Linux.",
                extra_context: str = ""):
    messages = [
        {"role": "system", "content": build_system_prompt(persona, extra_context)},
        {"role": "user", "content": task},
    ]
    call_history = []

    console.print(Panel.fit(
        f"[bold cyan]Agent[/]\nModel: [yellow]{model}[/]\nTask:  [green]{task}[/]\n"
        f"Tools: [dim]{', '.join(TOOLS.keys())}[/]", border_style="cyan"
    ))

    for iteration in range(1, MAX_ITERATIONS + 1):
        console.print(f"\n[bold blue]═══ Runde {iteration}/{MAX_ITERATIONS} ═══[/]")
        try:
            # Streaming: tokens vises live etter hvert som modellen genererer,
            # i stedet for aa vente paa hele svaret (bedre opplevd fart).
            msg = call_ollama(model, messages, tools=NATIVE_TOOLS, stream=True,
                              on_token=lambda t: (sys.stdout.write(t), sys.stdout.flush()))
            response = msg.get("content") or ""
        except Exception as e:
            console.print(f"[red]Feil ved Ollama-kall: {e}[/]")
            break

        if response:
            sys.stdout.write("\n")   # avslutt den streamede linjen rent
            sys.stdout.flush()

        # Native tool_calls har forrang; fall tilbake til <tool>-XML for
        # modeller som skriver formatet i teksten i stedet.
        tool_calls = []
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function", {}) or {}
            nm = fn.get("name", "")
            arguments = fn.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except Exception:
                    arguments = {"input": arguments}
            arg_str = arguments.get("input")
            if arg_str is None:
                arg_str = " ||| ".join(str(v) for v in arguments.values()) if arguments else ""
            tool_calls.append((nm, str(arg_str)))
        if not tool_calls:
            tool_calls = parse_tool_calls(response) or parse_json_tool_calls(response)
        msg["content"] = response
        messages.append(msg)

        # VIKTIG: en modell kan skrive et <tool>-kall OG dikte opp et resultat
        # OG <DONE> i samme svar (sett i praksis med llama3-agent). Hvis vi
        # respekterte <DONE> her ville vi avsluttet på det oppdiktede
        # resultatet uten å noensinne kjøre verktøyet. Et reelt verktøykall
        # har derfor ALLTID forrang over <DONE> i samme runde — vi kjører det
        # og tvinger modellen til å se det ekte resultatet før den kan
        # avslutte.
        if not tool_calls and "<DONE>" in response:
            console.print("\n[bold green]✅ Agenten er ferdig.[/]")
            break

        if not tool_calls:
            if interactive:
                user_input = console.input("\n[dim]Ingen verktøykall. Skriv instruks (eller 'avslutt'): [/]")
                if user_input.lower() in ("avslutt", "exit", "quit"):
                    break
                messages.append({"role": "user", "content": user_input})
            else:
                messages.append({"role": "user", "content": "DU MÅ bruke et verktøy. Skriv nøyaktig dette formatet:\n<tool name=\"VERKTØYNAVN\">\nargumenter\n</tool>\n\nEller skriv <DONE> hvis oppgaven er fullført."})
            continue

        call_sig = tuple((n, a[:50]) for n, a in tool_calls)
        call_history.append(call_sig)
        if call_history.count(call_sig) >= 3:
            console.print("[red]⚠️ Loop-deteksjon: samme verktøykall gjentatt 3 ganger.[/]")
            messages.append({"role": "user", "content": "Du gjentar det samme verktøykallet. Prøv en annen tilnærming eller skriv <DONE>."})
            if call_history.count(call_sig) >= 4:
                console.print("[red]⚠️ Fortsatt loop — stopper agenten.[/]")
                break
            continue

        feedback_parts = []
        for tool_name, tool_args in tool_calls:
            console.print(Panel(f"[bold yellow]{tool_name}[/]\n[dim]{tool_args[:200]}{'...' if len(tool_args) > 200 else ''}[/]",
                                 title="🔧 Verktøykall", border_style="yellow", expand=False))
            result = execute_tool(tool_name, tool_args)
            console.print(Panel(result[:1500] + ("..." if len(result) > 1500 else ""),
                                 title=f"📤 Resultat: {tool_name}", border_style="green", expand=False))
            feedback_parts.append(f"Verktøy: {tool_name}\nArgumenter: {tool_args}\n\nResultat:\n{result}")

        feedback = "\n\n---\n\n".join(feedback_parts)
        messages.append({"role": "user", "content": f"VERKTØYRESULTAT:\n\n{feedback}\n\nAnalyser resultatet og bestem neste steg, eller skriv <DONE> med en oppsummering."})
    else:
        console.print(f"\n[red]⚠️ Maks {MAX_ITERATIONS} runder nådd.[/]")
