"""CLI for managing canonical client identities and profiles."""
from __future__ import annotations
import argparse
import json
import shutil
from pathlib import Path
try:
    from .client_workspace import WorkspaceManager, name_key_from_filename
except ImportError:  # direct invocation from apps/sniper
    from client_workspace import WorkspaceManager, name_key_from_filename


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage Sniper multi-client workspaces")
    parser.add_argument("--root", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser(
        "init",
        help="Provision one named client, or all clients represented in _inbox when name is omitted",
    )
    p.add_argument(
        "name",
        nargs="?",
        help="Client name; omit to provision every client represented in _inbox",
    )
    p.add_argument("--id")
    p.add_argument("--credential-reference")
    p.add_argument("--crm-account-id")
    p.add_argument(
        "--refresh",
        action="store_true",
        help="Re-read shared .env credentials and refresh per-client fingerprints",
    )
    sub.add_parser("list")
    sub.add_parser("doctor")
    p = sub.add_parser("delete"); p.add_argument("client")
    p = sub.add_parser("assign"); p.add_argument("source"); p.add_argument("client")
    p = sub.add_parser("import"); p.add_argument("source")
    args = parser.parse_args(); manager = WorkspaceManager(args.root)
    if args.command == "init":
        def audit_registered(refresh: bool) -> list[str]:
            errors = manager.audit_credential_variables()
            registry_data = manager._load_registry().get("name_keys", {})
            for ids in registry_data.values():
                for client_id in ids if isinstance(ids, list) else [ids]:
                    paths = manager.client_paths(client_id)
                    if not paths.root.exists():
                        errors.append(
                            f"{client_id}: registered client directory is missing: {paths.root}"
                        )
                    elif not paths.profile_path.exists():
                        errors.append(
                            f"{client_id}: client profile is missing: {paths.profile_path}"
                        )
                    elif refresh:
                        values = manager.profile_values(client_id)
                        if not values.get("crm_account_id") or values["crm_account_id"].lower() == "null":
                            manager.set_crm_account_id(client_id, client_id)
                            print(
                                f"REPAIR {client_id}: crm_account_id set to canonical client ID",
                                flush=True,
                            )
            if refresh:
                for ids in registry_data.values():
                    for client_id in ids if isinstance(ids, list) else [ids]:
                        errors.extend(manager.credential_audit(client_id, refresh=True))
            return list(dict.fromkeys(errors))

        if not args.name:
            if args.id or args.credential_reference or args.crm_account_id:
                parser.error("--id, --credential-reference, and --crm-account-id require a client name")
            registry = manager._load_registry().get("name_keys", {})
            initialized = set()
            created_count = 0
            skipped_count = 0
            imported_count = 0
            failures = []
            if args.refresh:
                failures.extend(manager.refresh_job_source_names())
            inbox_files, discarded_files = manager.select_latest_inbox_files(
                manager.discover_inbox()
            )
            for discarded in discarded_files:
                rejected = manager.root / "_rejected" / discarded.name
                rejected.parent.mkdir(parents=True, exist_ok=True)
                if discarded.exists():
                    discarded.rename(rejected)
                print(f"REJECT {discarded.name}: older duplicate for the same client")
            for source in inbox_files:
                try:
                    name_key = name_key_from_filename(source.name)
                    embedded_client = manager.client_id_from_filename(source.name)
                    values = registry.get(name_key, [])
                    values = [values] if isinstance(values, str) else values
                    if len(values) > 1:
                        raise ValueError(
                            f"name_key {name_key!r} maps to multiple clients: {values}"
                        )
                    if embedded_client:
                        client_id = embedded_client
                        manager.validate_source_identity(source.name, client_id)
                        name_key = manager.profile_values(client_id).get("name_key", name_key)
                        skipped_count += 1
                        print(f"SKIP {source.name}: embedded client ID {client_id}")
                    elif values:
                        client_id = values[0]
                        skipped_count += 1
                        print(f"SKIP {source.name}: {client_id} already initialized")
                    else:
                        client_id = manager.resolve_canonical_client_id(name_key)
                        registry[name_key] = [client_id]
                        created_count += 1
                        print(f"INIT {source.name}: {client_id}")
                    if client_id not in initialized:
                        manager.ensure_client_provisioned(client_id, name_key)
                        initialized.add(client_id)
                    claimed = manager.claim_inbox_file(source)
                    if claimed is None:
                        print(f"SKIP {source.name}: it was claimed by another process")
                        continue
                    try:
                        workspace = manager.import_inbox(claimed, client_id=client_id)
                    except Exception:
                        rejected = manager.root / "_rejected" / claimed.name
                        rejected.parent.mkdir(parents=True, exist_ok=True)
                        if claimed.exists():
                            claimed.rename(rejected)
                        raise
                    imported_count += 1
                    print(f"IMPORT {source.name}: {workspace.job_id} -> {workspace.source_path}")
                except (OSError, ValueError, KeyError) as exc:
                    failures.append(f"{source.name}: {exc}")
                    print(f"ERROR {source.name}: {exc}")
                except Exception as exc:
                    failures.append(f"{source.name}: {exc}")
                    print(f"ERROR {source.name}: {exc}")
            failures.extend(audit_registered(args.refresh))
            for failure in failures:
                print(f"ERROR: {failure}")
            if failures:
                raise SystemExit(1)
            if not initialized:
                print("No Excel files found in clients\\_inbox.")
            else:
                print(
                    f"Processed {len(initialized)} client(s): "
                    f"{created_count} created, {skipped_count} skipped, "
                    f"{imported_count} file(s) imported."
                )
            return
        cid = args.id or manager.resolve_canonical_client_id(args.name)
        if args.id:
            manager.register_client(args.name, cid)
        manager.ensure_client_provisioned(cid, args.name, args.credential_reference)
        if args.credential_reference:
            manager.set_credential_reference(cid, args.credential_reference)
        if args.crm_account_id:
            manager.set_crm_account_id(cid, args.crm_account_id)
        if args.refresh:
            source_errors = manager.refresh_job_source_names()
            for error in source_errors:
                print(f"ERROR: {error}")
            if source_errors:
                raise SystemExit(1)
        errors = manager.credential_audit(cid, refresh=args.refresh)
        for error in errors:
            print(f"ERROR: {error}")
        if errors:
            raise SystemExit(1)
        print(cid)
    elif args.command == "list":
        for name_key, ids in sorted(manager._load_registry().get("name_keys", {}).items()):
            print(f"{name_key}\t{', '.join(ids) if isinstance(ids, list) else ids}")
    elif args.command == "doctor":
        problems = []
        seen_accounts = {}
        for name_key, ids in manager._load_registry().get("name_keys", {}).items():
            for cid in ids if isinstance(ids, list) else [ids]:
                values = manager.profile_values(cid)
                if values.get("enabled", "").lower() == "true":
                    if not values.get("crm_account_id") or values["crm_account_id"].lower() == "null":
                        problems.append(f"{cid}: enabled client has no crm_account_id")
                    if not values.get("credential_reference", "").startswith("env:"):
                        problems.append(f"{cid}: missing credential_reference")
                account = values.get("crm_account_id")
                if account and account.lower() != "null":
                    if account in seen_accounts:
                        problems.append(
                            f"{cid} and {seen_accounts[account]} share crm_account_id={account!r}"
                        )
                    seen_accounts[account] = cid
        if problems:
            for problem in problems:
                print(f"ERROR: {problem}")
            raise SystemExit(1)
        print("All clients passed profile and CRM-account checks.")
    elif args.command == "delete":
        cid = args.client
        if not (manager.root / cid).exists():
            cid = manager.resolve_canonical_client_id(args.client, allow_create=False)
        shutil.rmtree(manager.client_paths(cid).root)
        data = manager._load_registry()
        cleaned = {}
        for key, values in data.get("name_keys", {}).items():
            values = [values] if isinstance(values, str) else values
            remaining = [item for item in values if item != cid]
            if remaining:
                cleaned[key] = remaining
        data["name_keys"] = cleaned
        manager.registry_path.write_text(json.dumps(data, indent=2, sort_keys=True),
                                         encoding="utf-8")
    elif args.command == "assign":
        source = manager.root / "_inbox" / __import__("pathlib").Path(args.source).name
        shutil.copy2(args.source, source)
        print(manager.import_inbox(source, client_id=args.client).job_id)
    else:
        print(manager.import_inbox(args.source).job_id)


if __name__ == "__main__":
    main()
