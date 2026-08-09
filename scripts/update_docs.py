from coding_tools_mcp.server import TOOL_REGISTRY


def update_docs():
    with open("docs/tools-and-schemas.md", "r") as f:
        content = f.read()

    new_inventory = (
        "The default catalog contains exactly "
        + str(len(TOOL_REGISTRY))
        + " tools:\n\n"
    )
    for name, spec in TOOL_REGISTRY.items():
        new_inventory += f"- `{name}`: {spec.description}\n"

    # Replace the inventory list in the doc
    # We look for "The default catalog contains exactly" up to "## Result envelope"

    start_idx = content.find("The default catalog contains exactly")
    if start_idx == -1:
        print("Could not find start idx")
        return

    end_idx = content.find("`view_image` may be disabled", start_idx)
    if end_idx == -1:
        print("Could not find end idx")
        return

    new_content = content[:start_idx] + new_inventory + "\n" + content[end_idx:]

    with open("docs/tools-and-schemas.md", "w") as f:
        f.write(new_content)

    print("Updated docs/tools-and-schemas.md")


if __name__ == "__main__":
    update_docs()
