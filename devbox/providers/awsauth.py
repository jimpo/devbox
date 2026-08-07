"""Missing/expired AWS credentials, handled gracefully.

Every AWS-family provider (`ec2`, `aws`/Lightsail) builds its boto3 client
through `make_client`, which resolves the profile's credentials *before* the
first API call. That turns the common failure - an SSO session that lapsed
overnight - into a one-line explanation plus an offer to run `aws sso login`,
after which the command carries on, instead of a botocore stack trace.

Credentials that can only fail server-side (an expired static STS token, a
revoked access key) surface mid-command instead; `is_auth_error` duck-types
those too, so `cli.main` can turn them into the same one-line message.

Nothing here imports botocore: exceptions are recognised by class name (walking
the MRO, so subclasses count) and by ClientError code. That keeps boto3 lazily
imported for libvirt-only users and lets the tests use fakes.
"""
from __future__ import annotations

import configparser
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from ..config import AwsConfig
from ..util import die

# botocore exception classes that mean "no usable credentials". Matched against
# the whole MRO, so the SSO subclasses (SSOTokenLoadError,
# UnauthorizedSSOTokenError - the expired-SSO-session one) come along with
# SSOError, and TokenRetrievalError covers the OIDC refresh failing.
AUTH_EXCEPTION_NAMES = {
    "NoCredentialsError",
    "PartialCredentialsError",
    "CredentialRetrievalError",
    "TokenRetrievalError",
    "SSOError",
}

# ClientError codes meaning the request was rejected for *who we are*, not what
# we asked for - i.e. credentials that resolved locally but are stale or bogus.
# ("UnauthenticatedException" is Lightsail's spelling of the same thing.)
AUTH_ERROR_CODES = {
    "ExpiredToken",
    "ExpiredTokenException",
    "RequestExpired",
    "InvalidClientTokenId",
    "UnrecognizedClientException",
    "AuthFailure",
    "InvalidAccessKeyId",
    "SignatureDoesNotMatch",
    "UnauthenticatedException",
}

DEFAULT_AWS_CONFIG_PATH = Path.home() / ".aws" / "config"


class AwsAuthError(Exception):
    """Missing/expired AWS credentials the user has to fix. The message is
    complete and user-facing (it already names the login command), so callers
    print it as-is - `cli.main` turns it into a plain `error: ...` line, and
    best-effort callers (`_check_name_unique`) degrade it to a warning."""


def error_code(err: Exception) -> Optional[str]:
    """boto3/botocore ClientError code, duck-typed (no botocore import needed)."""
    response = getattr(err, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code")
    return None


def _exception_names(err: Exception) -> set:
    return {cls.__name__ for cls in type(err).__mro__}


def is_auth_error(err: Exception) -> bool:
    """Whether `err` is boto's way of saying the credentials are missing or no
    longer valid (as opposed to a real API/permission failure)."""
    if _exception_names(err) & AUTH_EXCEPTION_NAMES:
        return True
    return error_code(err) in AUTH_ERROR_CODES


def _aws_config_path() -> Path:
    env = os.environ.get("AWS_CONFIG_FILE")
    return Path(env).expanduser() if env else DEFAULT_AWS_CONFIG_PATH


def effective_profile(aws: AwsConfig) -> Optional[str]:
    """The profile boto3 will actually use: `[aws] profile`, else $AWS_PROFILE,
    else none (the `default` section)."""
    return aws.profile or os.environ.get("AWS_PROFILE") or None


def describe_profile(aws: AwsConfig) -> str:
    profile = effective_profile(aws)
    return f"profile '{profile}'" if profile else "the default AWS profile"


def _profile_uses_sso(profile: Optional[str]) -> bool:
    """Whether ~/.aws/config configures this profile for SSO - the only case
    where devbox knows a login command that would actually help (static keys and
    instance roles can't be refreshed by anything we could run)."""
    path = _aws_config_path()
    if not path.is_file():
        return False
    parser = configparser.RawConfigParser()
    try:
        parser.read(path)
    except configparser.Error:
        return False
    section = f"profile {profile}" if profile else "default"
    if not parser.has_section(section):
        return False
    return any(key.startswith("sso_") for key in parser.options(section))


def login_command(aws: AwsConfig) -> Optional[list]:
    """The argv that refreshes this profile's credentials, or None if there is
    no such command (non-SSO profiles: the user has to sort it out themselves)."""
    profile = effective_profile(aws)
    if not _profile_uses_sso(profile):
        return None
    cmd = ["aws", "sso", "login"]
    if profile:
        cmd += ["--profile", profile]
    return cmd


def _reason(err: Exception) -> str:
    """A short one-line summary of a boto auth failure for the message we print.
    The SSO errors append a "to refresh, run aws sso login" sentence that we're
    about to say better ourselves, so keep only the first sentence."""
    text = " ".join(str(err).split()) or type(err).__name__
    first = text.split(". ")[0]
    return first if len(first) <= 160 else first[:157] + "..."


def credential_problem(session) -> Optional[str]:
    """A one-line reason `session`'s credentials are unusable, or None if they
    resolve. `get_frozen_credentials()` forces the resolution boto3 defers - an
    SSO profile only notices its cached token is stale when the credentials are
    actually needed - which is the same work the first API call would do, just
    early enough to do something about it. Non-auth failures propagate."""
    try:
        creds = session.get_credentials()
        if creds is None:
            return "no credentials found"
        creds.get_frozen_credentials()
    except Exception as err:  # noqa: BLE001 - re-raised unless it's an auth failure
        if not is_auth_error(err):
            raise
        return _reason(err)
    return None


def prompt_login(aws: AwsConfig, reason: str, stdin=None) -> None:
    """Explain the credential problem and, on a TTY, offer to run the login.
    Returns once a login has run (whether it actually helped is the caller's to
    re-check); raises AwsAuthError - message ready to print - when there is no
    login to offer, no TTY to ask on, or the user declines. `stdin` defaults to
    the live sys.stdin, resolved at call time so it's patchable."""
    where = describe_profile(aws)
    prefix = f"AWS credentials for {where} are missing or expired ({reason})"
    cmd = login_command(aws)
    if cmd is None:
        raise AwsAuthError(f"{prefix}; refresh them and re-run")
    printable = " ".join(cmd)
    if shutil.which(cmd[0]) is None:
        raise AwsAuthError(f"{prefix}; run `{printable}` and re-run "
                           f"(the `aws` CLI is not on $PATH)")
    if stdin is None:
        stdin = sys.stdin
    if not stdin.isatty():
        raise AwsAuthError(f"{prefix}; run `{printable}` and re-run")

    print(f"{prefix}.")
    if input(f"Run `{printable}` now? [Y/n] ").strip().lower() not in ("", "y", "yes"):
        raise AwsAuthError(f"aborted; run `{printable}` and try again")
    print(f"==> {printable}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise AwsAuthError(f"`{printable}` failed (exit {result.returncode})")


def ensure_credentials(new_session, aws: AwsConfig, stdin=None):
    """Return a boto3 session with usable credentials, prompting for a login
    first if the initial one's are missing or expired. `new_session` is a
    zero-arg factory rather than a session: a session caches whatever it
    resolved, so the post-login re-check has to run against a fresh one."""
    session = new_session()
    problem = credential_problem(session)
    if problem is None:
        return session

    prompt_login(aws, problem, stdin=stdin)

    session = new_session()
    problem = credential_problem(session)
    if problem is not None:
        raise AwsAuthError(f"AWS credentials for {describe_profile(aws)} are still "
                           f"unusable after logging in ({problem})")
    return session


def make_client(aws: AwsConfig, service: str, label: str):
    """The boto3 client every AWS-family provider goes through: profile/region
    from `[aws]`, credentials resolved (and refreshed if need be) up front."""
    try:
        import boto3
    except ImportError:
        die(f"boto3 is required for the {label}: pip install boto3 "
            f"(or `uv tool install --with boto3 devbox`)")

    def new_session():
        return boto3.Session(profile_name=aws.profile, region_name=aws.region)

    try:
        session = ensure_credentials(new_session, aws)
    except Exception as err:  # noqa: BLE001 - only ProfileNotFound is handled here
        if "ProfileNotFound" in _exception_names(err):
            die(f"AWS profile '{effective_profile(aws)}' is not configured in "
                f"{_aws_config_path()}; fix `profile` under [aws] in the devbox "
                f"global config, or set the profile up (`aws configure sso`)")
        raise
    return session.client(service)


def auth_error_message(aws: AwsConfig, err: Exception) -> str:
    """The message for an auth failure that only surfaced once a command was
    already running (server-side rejection, or a token that expired mid-command).
    Too late to prompt and resume, so it just says what to run."""
    prefix = (f"AWS credentials for {describe_profile(aws)} are expired or invalid "
              f"({_reason(err)})")
    cmd = login_command(aws)
    if cmd is None:
        return f"{prefix}; refresh them and re-run"
    return f"{prefix}; run `{' '.join(cmd)}` and re-run"


__all__ = [
    "AwsAuthError", "auth_error_message", "credential_problem", "describe_profile",
    "effective_profile", "ensure_credentials", "error_code", "is_auth_error",
    "login_command", "make_client", "prompt_login",
]
