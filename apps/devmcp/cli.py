import sys
import argparse
from coding_tools_mcp.approval import ApprovalEngine
from datetime import datetime

def main():
    parser = argparse.ArgumentParser(prog="devmcp")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    subparsers.add_parser("approvals", help="List pending approvals")
    
    parser_show = subparsers.add_parser("show", help="Show approval details")
    parser_show.add_argument("id")
    
    parser_approve = subparsers.add_parser("approve", help="Approve request")
    parser_approve.add_argument("id")
    parser_approve.add_argument("--once", action="store_true")
    parser_approve.add_argument("--pattern")
    
    parser_deny = subparsers.add_parser("deny", help="Deny request")
    parser_deny.add_argument("id")
    
    args = parser.parse_args()
    engine = ApprovalEngine()
    
    if args.command == "approvals":
        pending = engine.list_pending()
        if not pending:
            print("No pending approvals.")
        else:
            for req in pending:
                print(f"[{req['id']}] {req['command_or_action']} in {req['working_directory']}")
                
    elif args.command == "show":
        reqs = engine.list_pending()
        req = next((r for r in reqs if r["id"] == args.id), None)
        if not req:
            print(f"Request {args.id} not found or not pending.")
            sys.exit(1)
        for k, v in req.items():
            print(f"{k}: {v}")
        
    elif args.command == "approve":
        engine.approve(args.id, pattern=args.pattern)
        if args.pattern:
            print(f"Approved {args.id} and saved pattern: {args.pattern}")
        else:
            print(f"Approved {args.id} (once)")
        
    elif args.command == "deny":
        engine.deny(args.id)
        print(f"Denied {args.id}")

if __name__ == "__main__":
    main()
