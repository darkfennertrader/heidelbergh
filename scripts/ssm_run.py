#!/usr/bin/env python3
"""
Run a PowerShell script on an AppWay Windows EC2 via SSM and print the output.

Usage:
    scripts/ssm_run.py <powershell-script-string>
    echo "Get-Service | Select-Object -First 5" | scripts/ssm_run.py -

    # Target a different EC2 (e.g. HEYEX 2):
    scripts/ssm_run.py --instance i-02a7dd1797d85a099 -

    # Full override:
    scripts/ssm_run.py --instance i-xxxx --region us-east-1 "Get-Date"

Options:
    --instance <id>   EC2 instance ID (default: AppWay backend EC2)
    --region <name>   AWS region (default: eu-west-1)

Requirements: boto3, role with ssm:SendCommand / ssm:GetCommandInvocation
"""
import sys
import time
import argparse
import boto3

# ── defaults (AppWay backend EC2) ────────────────────────────────────────────
DEFAULT_INSTANCE_ID = "i-02a99abeba370f0a7"
DEFAULT_REGION      = "eu-west-1"

# ── known EC2 aliases ─────────────────────────────────────────────────────────
ALIASES = {
    "heyex2":   "i-02a7dd1797d85a099",
    "backend":  "i-02a99abeba370f0a7",
}


def main() -> int:
    parser = argparse.ArgumentParser(add_help=True, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("script", nargs="?", default=None,
                        help="PowerShell script string, or '-' to read from stdin")
    parser.add_argument("--instance", "-i", default=DEFAULT_INSTANCE_ID,
                        help=f"EC2 instance ID or alias (heyex2, backend). Default: {DEFAULT_INSTANCE_ID}")
    parser.add_argument("--region", "-r", default=DEFAULT_REGION,
                        help=f"AWS region. Default: {DEFAULT_REGION}")

    # Be tolerant: allow the old positional-only calling convention
    args, unknown = parser.parse_known_args()

    # Resolve alias
    instance_id = ALIASES.get(args.instance, args.instance)

    if args.script is None and not unknown:
        print(__doc__, file=sys.stderr)
        return 2

    if args.script == "-" or (args.script is None and not sys.stdin.isatty()):
        script = sys.stdin.read()
    elif args.script is not None:
        script = args.script
    elif unknown:
        script = " ".join(unknown)
    else:
        print(__doc__, file=sys.stderr)
        return 2

    ssm = boto3.client("ssm", region_name=args.region)
    resp = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunPowerShellScript",
        Parameters={"commands": [script]},
        TimeoutSeconds=120,
    )
    command_id = resp["Command"]["CommandId"]
    print(f"[ssm] Instance={instance_id}  CommandId={command_id}", file=sys.stderr)

    # Poll for completion
    for _ in range(60):
        time.sleep(2)
        inv = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        status = inv["Status"]
        if status in ("Success", "Failed", "Cancelled", "TimedOut"):
            print(f"[ssm] Status={status}", file=sys.stderr)
            out = inv.get("StandardOutputContent", "")
            err = inv.get("StandardErrorContent", "")
            if out:
                print(out)
            if err:
                print("--- STDERR ---", file=sys.stderr)
                print(err, file=sys.stderr)
            return 0 if status == "Success" else 1
    print("[ssm] Timed out waiting for command.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
