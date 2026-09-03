#!/usr/bin/env python3
"""
Llama3 Self-Improving Agent — tynn CLI-wrapper rundt agent_core.

All verktøylogikk bor i agent_core.py. Denne filen setter bare hvilken
Ollama-modell som skal brukes, og eksponerer samme kommandolinje-grensesnitt
som før:

    python3 agent_llama3.py "oppgave"
    python3 agent_llama3.py                    # interaktiv
    python3 agent_llama3.py --tools            # list tilgjengelige verktøy
    python3 agent_llama3.py --analyze /path    # analyser katalog direkte
    python3 agent_llama3.py --improve "topic"  # selvforbedringsmodus direkte
"""

import sys
sys.path.insert(0, "/home/kali/AI")
import agent_core as core

MODEL = "llama3.2:3b"  # 3B: 100% paa GPU (fart) + native tool-calling (paalitelighet). 8B via: agent_run.py --model llama3-agent
core.MAX_TOOL_OUTPUT = 4000  # llama3 har mindre kontekst enn WhiteRabbitNeo

PERSONA = "Du er en autonom, selvforbedrende AI-agent kjørende på Kali Linux med ekte verktøytilgang."

def main():
    if "--tools" in sys.argv:
        core.show_tools()
        return

    if "--analyze" in sys.argv:
        idx = sys.argv.index("--analyze")
        path = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else "."
        core.console.print(core.tool_analyze_directory(path))
        return

    if "--improve" in sys.argv:
        idx = sys.argv.index("--improve")
        topic = " ".join(sys.argv[idx + 1:]) if idx + 1 < len(sys.argv) else ""
        core.console.print(f"[bold cyan]Selvforbedringsmodus: {topic}[/]\n")
        core.console.print(core.tool_self_improve(topic))
        return

    if len(sys.argv) > 1:
        task = " ".join(a for a in sys.argv[1:] if a != "--tools")
        core.agent_loop(MODEL, task, interactive=False, persona=PERSONA)
    else:
        core.console.print("[bold cyan]Llama3 Self-Improving Agent — Interaktiv[/]\n")
        core.show_tools()
        core.console.print()
        while True:
            task = core.console.input("[bold green]oppgave>[/] ")
            if task.lower() in ("avslutt", "exit", "quit"):
                break
            if task.strip():
                core.agent_loop(MODEL, task, interactive=True, persona=PERSONA)

if __name__ == "__main__":
    main()
