#!/usr/bin/env python3
"""
Generisk agent-runner — kjører EN HVILKEN SOM HELST Ollama-modell gjennom den
ekte verktøy-løkken i agent_core.py, i stedet for en rå `ollama run` (som ikke
kan utføre noe — modellen kan bare late som).

Dette er det som kobler orchestrated-coder / glm-5.2:cloud-personaen (som i
sin Modelfile-systemprompt hevder å ha bash/fil/web-verktøy og skills) til
faktisk fungerende verktøy. Uten denne runneren var det ingenting som fanget
opp <tool name="..."> fra den modellen — den kunne bare dikte opp resultater.

Bruk:
    python3 agent_run.py --model MODELLNAVN "oppgave"
    python3 agent_run.py --model MODELLNAVN            # interaktiv
    python3 agent_run.py --tools                        # list verktøy
"""

import argparse
import sys
sys.path.insert(0, "/home/kali/AI")
import agent_core as core

PERSONA = "Du er en autonom AI-agent kjørende på Kali Linux med ekte verktøytilgang (se listen under)."

def main():
    parser = argparse.ArgumentParser(description="Generisk agent-runner med ekte verktøy")
    parser.add_argument("--model", default="orchestrated-coder:latest", help="Ollama-modellnavn")
    parser.add_argument("--tools", action="store_true", help="List tilgjengelige verktøy og avslutt")
    parser.add_argument("task", nargs="*", help="Oppgaven som skal utføres")
    args = parser.parse_args()

    if args.tools:
        core.show_tools()
        return

    task = " ".join(args.task).strip()
    if task:
        core.agent_loop(args.model, task, interactive=False, persona=PERSONA)
    else:
        core.console.print(f"[bold cyan]Agent Runner — Interaktiv (modell: {args.model})[/]\n")
        core.show_tools()
        core.console.print()
        while True:
            t = core.console.input("[bold green]oppgave>[/] ")
            if t.lower() in ("avslutt", "exit", "quit"):
                break
            if t.strip():
                core.agent_loop(args.model, t, interactive=True, persona=PERSONA)

if __name__ == "__main__":
    main()
