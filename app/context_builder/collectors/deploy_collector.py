"""
DeployCollector — coleta informações sobre deploys recentes.

Fonte primária: git log nos repositórios conhecidos.
Correlaciona timestamp do incidente com deploys recentes.
"""

from __future__ import annotations

import subprocess
import os
from datetime import datetime, timezone, timedelta
from typing import Any

from app.context_builder.collectors.base import BaseCollector

# Repositórios a verificar por serviço
_SERVICE_REPOS: dict[str, list[str]] = {
    "data-core":     ["~/Documents/Projetos/data-core"],
    "poupi-crypto":  ["~/Documents/Projetos/poupi-crypto"],
    "poupi-baby":    ["~/Documents/Projetos/poupi-baby"],
}

_GIT_LOG_FORMAT = "%H|%an|%s|%ci"  # hash|author|subject|date
_MAX_COMMITS = 5


class DeployCollector(BaseCollector):
    name = "deploy"
    timeout_seconds = 5.0

    def collect_data(self, context: dict[str, Any]) -> dict[str, Any]:
        service = context.get("service", "")
        fired_at: datetime | None = context.get("fired_at")

        repos = _SERVICE_REPOS.get(service, _SERVICE_REPOS.get("data-core", []))
        all_commits: list[dict[str, Any]] = []
        recent_deploy: dict[str, Any] | None = None
        errors: list[str] = []

        for repo_path in repos:
            expanded = os.path.expanduser(repo_path)
            if not os.path.isdir(expanded):
                continue
            try:
                result = subprocess.run(
                    [
                        "git", "-C", expanded, "log",
                        f"--format={_GIT_LOG_FORMAT}",
                        f"-{_MAX_COMMITS}",
                        "--no-walk",
                        "--all",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds - 0.5,
                )
                if result.returncode == 0:
                    for line in result.stdout.strip().splitlines():
                        parts = line.split("|", 3)
                        if len(parts) == 4:
                            commit = {
                                "hash": parts[0][:12],
                                "author": parts[1],
                                "subject": parts[2][:100],
                                "date": parts[3],
                                "repo": os.path.basename(expanded),
                            }
                            all_commits.append(commit)
            except (subprocess.TimeoutExpired, OSError) as exc:
                errors.append(f"{repo_path}: {exc}")

        # Detectar se houve deploy recente (< 30 min antes do incidente)
        if fired_at and all_commits:
            cutoff = fired_at - timedelta(minutes=30)
            for commit in all_commits:
                try:
                    commit_dt = datetime.fromisoformat(commit["date"].replace(" ", "T", 1))
                    if commit_dt.tzinfo is None:
                        commit_dt = commit_dt.replace(tzinfo=timezone.utc)
                    if commit_dt >= cutoff:
                        recent_deploy = commit
                        break
                except (ValueError, TypeError):
                    pass

        return {
            "service": service,
            "recent_commits": all_commits,
            "recent_deploy_before_incident": recent_deploy,
            "deploy_possibly_caused_incident": recent_deploy is not None,
            "errors": errors,
        }
