#!/usr/bin/env python3
"""Run kubesupport from a terminal, using your current kubeconfig.

    python cli.py scan                         # pods that ever restarted or are failing
    python cli.py incidents                    # what the watcher would alert on right now
    python cli.py rca -q "why is web failing?" # full RCA (Claude if ANTHROPIC_API_KEY is set)
    python cli.py rca -n default -p web-7d9f8b-abcde --teams
    python cli.py teams-test                   # send a sample card to TEAMS_ALERT_WEBHOOK_URLS
"""
import argparse
import json

import agent
import cards
import report
import teams
import tools


def main():
    parser = argparse.ArgumentParser(description="kubesupport - Kubernetes root cause analysis")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("scan", help="List pods that are failing or have restarted")
    sub.add_parser("incidents", help="List incidents the watcher would alert on now")

    rca_p = sub.add_parser("rca", help="Diagnose and print an RCA")
    rca_p.add_argument("--question", "-q", default="Investigate failing pods in the cluster and create an RCA")
    rca_p.add_argument("--namespace", "-n", help="Namespace of the pod to diagnose")
    rca_p.add_argument("--pod", "-p", help="Pod to diagnose")
    rca_p.add_argument("--teams", action="store_true", help="Also post the RCA card to the Teams alert webhooks")

    sub.add_parser("teams-test", help="Send a sample card to the Teams alert webhooks")

    args = parser.parse_args()

    if args.command == "scan":
        issues = tools.scan_cluster_for_issues()
        print(json.dumps(issues, indent=2) if issues else "All pods in the watched namespaces look healthy.")

    elif args.command == "incidents":
        found, _ = tools.detect_incidents({})
        print(json.dumps(list(found.values()), indent=2) if found else "No open incidents.")

    elif args.command == "rca":
        target = None
        if args.namespace and args.pod:
            target = {"namespace": args.namespace, "pod": args.pod, "workload": None,
                      "reason": "requested from CLI", "detail": ""}
        data = agent.investigate_and_rca(args.question, target=target)
        filename, _ = report.render_rca(data)
        print(f"Report:     {filename}")
        print(f"Severity:   {data.get('severity')}")
        print(f"Title:      {data.get('title')}")
        print(f"Root cause: {data.get('root_cause')}")
        print(f"Engine:     {data.get('generated_by')}")
        if data.get("llm_error"):
            print(f"LLM error (fell back to rules): {data['llm_error']}")
        if args.teams:
            print(f"Teams:      {teams.post_alert(cards.rca_card(data))}")

    elif args.command == "teams-test":
        print(teams.post_alert(cards.test_card()) or "TEAMS_ALERT_WEBHOOK_URLS is not set.")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
