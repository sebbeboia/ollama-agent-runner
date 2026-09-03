#!/usr/bin/env python3
"""
WhiteRabbitNeo Multi-Tool Agent — tynn CLI-wrapper rundt agent_core.

All verktøylogikk bor i agent_core.py. Denne filen setter bare hvilken
Ollama-modell som skal brukes og en persona-beskrivelse, og eksponerer
samme kommandolinje-grensesnitt som før:

    python3 agent.py "oppgave"
    python3 agent.py                    # interaktiv
    python3 agent.py --tools            # list tilgjengelige verktøy
"""

import sys
sys.path.insert(0, "/home/kali/AI")
import agent_core as core

MODEL = "hf.co/bartowski/WhiteRabbitNeo-2.5-Qwen-2.5-Coder-7B-GGUF:Q4_K_M"

PERSONA = "Du er WhiteRabbitNeo, en autonom multi-tool agent kjørende på Kali Linux, spesialisert på pentest og sikkerhetsarbeid."

EXTRA_CONTEXT = """## LÆRINGSYKLUS

Når du løser en oppgave:
1. Sjekk om du har relevant lærdom fra før (recall)
2. Se om det finnes en relevant skill (list_skills, så skill NAVN)
3. Søk på nett hvis du mangler kunnskap (web_search + web_fetch)
4. Kjør verktøy/kommandoer (bash / orchestrate)
5. Lagre viktige funn for fremtiden (learn)
6. Oppsummer og avslutt med <DONE>

## METODIKK-REFERANSE

Detaljert PTES/OWASP/MITRE-ATT&CK-sjekkliste finnes på /home/kali/Pentest/PENTEST-METHODOLOGY.md
— bruk read_file/grep_file på DEN filen for metodikk, ikke agent_memory/knowledge_base.md
(den filen er kun en fri-tekst logg over egne læringer via `learn`, ikke en forhåndsskrevet
kunnskapsbase — ikke anta at den inneholder noe du ikke selv har lagret der).
"""

def main():
    if "--tools" in sys.argv:
        core.show_tools()
        return
    if len(sys.argv) > 1:
        task = " ".join(a for a in sys.argv[1:] if a != "--tools")
        core.agent_loop(MODEL, task, interactive=False, persona=PERSONA, extra_context=EXTRA_CONTEXT)
    else:
        core.console.print("[bold cyan]WhiteRabbitNeo Multi-Tool Agent — Interaktiv[/]\n")
        core.show_tools()
        core.console.print()
        while True:
            task = core.console.input("[bold green]oppgave>[/] ")
            if task.lower() in ("avslutt", "exit", "quit"):
                break
            if task.strip():
                core.agent_loop(MODEL, task, interactive=True, persona=PERSONA, extra_context=EXTRA_CONTEXT)

if __name__ == "__main__":
    main()
