"""06: stored prompts injected by id, and Agent Skills published and read back.

An operator commits a prompt version; the application resolves the name once and sends the
id (and version) as ``Options``: the gateway prepends that version's messages. A skill is
published with its files and read back by name. Offline.

    uv run python examples/06_prompts_and_skills.py
"""

import asyncio

from _fake_gateway import ADMIN_TOKEN, KEY, URL, FakeGateway

from bifrost_sdk import Bifrost, Options
from bifrost_sdk.admin import Admin


async def main() -> None:
    with FakeGateway():
        async with Admin(URL, token=ADMIN_TOKEN) as admin:
            prompt = await admin.prompts.create("triage")
            v1 = await admin.prompts.commit(
                prompt.id,
                [{"role": "system", "content": "You triage tickets"}],
                model="provider/model",
                message="first cut",
            )
            print(f"prompt {prompt.name!r} version {v1.number}, latest={v1.is_latest}")

            skill = await admin.skills.create(
                "sql-review",
                description="Reviews SQL",
                body="# SQL review\nRead references/rules.md first.",
                version="1.0.0",
                files={"references/rules.md": "No SELECT *."},
            )
            found = await admin.skills.find("sql-review")
            assert found is not None and found.id == skill.id
            rules = await admin.skills.read_file("sql-review", "references/rules.md")
            print(f"skill {found.name} {found.version}: {[f.path for f in found.files]} {rules!r}")

            resolved = await admin.prompts.find("triage")  # names to ids, once
            assert resolved is not None

        async with Bifrost(f"{URL}/v1", model="provider/model", api_key=KEY) as bf:
            options = Options(prompt_id=resolved.id, prompt_version=v1.number)
            print("with the stored prompt:", await bf.chat("Printer on fire", options=options))


if __name__ == "__main__":
    asyncio.run(main())
