import argparse
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_DIR = ROOT / "docs" / "mail-e2e-artifacts"


@dataclass
class AttachmentSpec:
    filename: str
    lines: list[str]


@dataclass
class MailScenario:
    key: str
    subject: str
    from_header: str
    attachments: list[AttachmentSpec]
    note: str


def make_pdf(path: Path, lines: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=A4)
    y = 780
    for line in lines:
        pdf.drawString(56, y, line)
        y -= 20
    pdf.save()


def build_default_scenarios() -> list[MailScenario]:
    return [
        MailScenario(
            key="01-new-case-mila",
            subject="VNZH intake: Mila Petrova passport",
            from_header="Mila Petrova <mila.petrova@example.com>",
            note="Should create a new case for Mila Petrova.",
            attachments=[
                AttachmentSpec(
                    filename="passport-mila-petrova.pdf",
                    lines=[
                        "Passport",
                        "Name: Mila Petrova",
                        "Passport number: Y76543210",
                        "Date of birth: 12.09.1993",
                        "Nationality: Russian",
                        "Destination: Germany",
                    ],
                )
            ],
        ),
        MailScenario(
            key="02-follow-up-contract-mila",
            subject="VNZH follow-up: Mila Petrova employment contract",
            from_header="Relocation Partner <assistant@relocation-partner.example>",
            note="Should attach to the existing Mila Petrova case by identity fields.",
            attachments=[
                AttachmentSpec(
                    filename="employment-contract-mila-petrova.pdf",
                    lines=[
                        "Employment contract",
                        "Name: Mila Petrova",
                        "Passport number: Y76543210",
                        "Date of birth: 12.09.1993",
                        "Employer: Nordlicht GmbH",
                    ],
                )
            ],
        ),
        MailScenario(
            key="03-duplicate-passport-mila",
            subject="Duplicate passport copy for Mila Petrova",
            from_header="Mila Petrova <mila.petrova@example.com>",
            note="Should be marked as duplicate if the first passport was already processed.",
            attachments=[
                AttachmentSpec(
                    filename="passport-mila-petrova-duplicate.pdf",
                    lines=[
                        "Passport",
                        "Name: Mila Petrova",
                        "Passport number: Y76543210",
                        "Date of birth: 12.09.1993",
                        "Nationality: Russian",
                        "Destination: Germany",
                    ],
                )
            ],
        ),
        MailScenario(
            key="04-weak-scan-review",
            subject="Scanned document",
            from_header="Unknown Sender <unknown.sender@example.com>",
            note="Should stay in manual review or unrecognized because identity is too weak.",
            attachments=[
                AttachmentSpec(
                    filename="scan-001.pdf",
                    lines=[
                        "Scan copy",
                        "Please see attached",
                        "No clear identity data here",
                    ],
                )
            ],
        ),
        MailScenario(
            key="05-new-case-oleg",
            subject="VNZH intake: Oleg Sidorov passport",
            from_header="Oleg Sidorov <oleg.sidorov@example.com>",
            note="Should create a second new case for Oleg Sidorov.",
            attachments=[
                AttachmentSpec(
                    filename="passport-oleg-sidorov.pdf",
                    lines=[
                        "Passport",
                        "Name: Oleg Sidorov",
                        "Passport number: M12390877",
                        "Date of birth: 03.04.1990",
                        "Nationality: Belarusian",
                        "Destination: Germany",
                    ],
                )
            ],
        ),
    ]


def render_scenario_files(scenarios: list[MailScenario]) -> dict[str, list[Path]]:
    written: dict[str, list[Path]] = {}
    for scenario in scenarios:
        paths: list[Path] = []
        scenario_dir = ARTIFACT_DIR / scenario.key
        for attachment in scenario.attachments:
            path = scenario_dir / attachment.filename
            make_pdf(path, attachment.lines)
            paths.append(path)
        written[scenario.key] = paths
    return written


def build_message(auth_user: str, inbox_email: str, scenario: MailScenario, attachment_paths: list[Path]) -> EmailMessage:
    message = EmailMessage()
    message["From"] = scenario.from_header
    message["To"] = inbox_email
    message["Subject"] = scenario.subject
    message["X-LexFlow-Scenario"] = scenario.key
    message["Reply-To"] = auth_user
    message.set_content(
        "\n".join(
            [
                "LexFlow intake live test pack.",
                f"Scenario: {scenario.key}",
                f"Expectation: {scenario.note}",
            ]
        )
    )
    for path in attachment_paths:
        message.add_attachment(
            path.read_bytes(),
            maintype="application",
            subtype="pdf",
            filename=path.name,
        )
    return message


def send_messages(
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    inbox_email: str,
    scenarios: list[MailScenario],
    attachment_paths: dict[str, list[Path]],
) -> None:
    if smtp_port == 465:
        smtp = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30)
    else:
        smtp = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
    with smtp:
        if smtp_port != 465:
            smtp.starttls()
        smtp.login(smtp_user, smtp_password)
        for scenario in scenarios:
            message = build_message(smtp_user, inbox_email, scenario, attachment_paths[scenario.key])
            smtp.send_message(message, from_addr=smtp_user, to_addrs=[inbox_email])
            print(f"SENT {scenario.key} -> {inbox_email}")
            print(f"  from: {scenario.from_header}")
            print(f"  subject: {scenario.subject}")
            for path in attachment_paths[scenario.key]:
                print(f"  attachment: {path}")
            print(f"  expected: {scenario.note}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and send a live LexFlow intake mail test pack.")
    parser.add_argument("--smtp-host", required=True)
    parser.add_argument("--smtp-port", type=int, default=465)
    parser.add_argument("--smtp-user", required=True)
    parser.add_argument("--smtp-password", required=True)
    parser.add_argument("--inbox-email", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    scenarios = build_default_scenarios()
    attachment_paths = render_scenario_files(scenarios)

    print(f"Artifacts: {ARTIFACT_DIR}")
    for scenario in scenarios:
        print(f"{scenario.key}: {scenario.note}")

    if args.dry_run:
        print("Dry run only. No email sent.")
        return

    send_messages(
        smtp_host=args.smtp_host,
        smtp_port=args.smtp_port,
        smtp_user=args.smtp_user,
        smtp_password=args.smtp_password,
        inbox_email=args.inbox_email,
        scenarios=scenarios,
        attachment_paths=attachment_paths,
    )


if __name__ == "__main__":
    main()
