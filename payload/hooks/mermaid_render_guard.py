"""PostToolUse(Edit|Write): рендерит изменённый .mmd через mermaid-cli; синтаксическая ошибка -> exit 2."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.stderr.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")

payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
path = (payload.get("tool_input") or {}).get("file_path") or ""
if not path.lower().endswith(".mmd") or not os.path.isfile(path):
    sys.exit(0)

mmdc = shutil.which("mmdc") or shutil.which("mmdc.cmd")
if not mmdc:
    print(f"mermaid_render_guard: mmdc не найден, {path} не проверен рендером.", file=sys.stderr)
    sys.exit(2)

out = os.path.join(tempfile.gettempdir(), "mermaid_render_guard.svg")
proc = subprocess.run([mmdc, "-q", "-i", path, "-o", out], capture_output=True, text=True,
                      encoding="utf-8", errors="replace", timeout=120)
if proc.returncode != 0:
    lines = [l for l in (proc.stderr + proc.stdout).splitlines() if l.strip()][:6]
    print(f"mermaid_render_guard: {path} не рендерится (mmdc exit {proc.returncode}).\n" + "\n".join(lines)
          + "\nЧастая причина: ';' внутри Note/подписи в sequenceDiagram.", file=sys.stderr)
    sys.exit(2)

print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": f"mmdc: {os.path.basename(path)} рендерится. Синтаксис OK не значит читаемо: "
                         "перед сдачей отрендерить в PNG и посмотреть глазами (наложения, обрезанный текст, лапша рёбер)."
}}, ensure_ascii=False))
sys.exit(0)
