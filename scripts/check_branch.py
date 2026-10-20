"""Enforce the repository's PR direction policy."""

import os

base = os.environ["BASE_BRANCH"]
head = os.environ["HEAD_BRANCH"]
if not (
    (base == "main" and head == "develop")
    or (base == "develop" and head not in {"main", "develop"})
):
    raise SystemExit("Only feature -> develop and develop -> main pull requests are allowed")
