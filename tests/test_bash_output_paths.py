from packages.core.ai.runtime.bash_output_paths import (
    BashOutputPathError,
    scope_bash_output_command,
)


def test_scopes_new_redirection_under_workspace_artifact_base(tmp_path):
    result = scope_bash_output_command(
        "echo hello > report.txt",
        artifact_base_dir="Workspaces/_by_id/folder/tasks/task",
        entity_root=str(tmp_path),
    )

    assert result.output_paths == ("Workspaces/_by_id/folder/tasks/task/artifacts/report.txt",)
    assert "Workspaces/_by_id/folder/tasks/task/artifacts/report.txt" in result.command


def test_keeps_entity_root_when_no_workspace_scope(tmp_path):
    result = scope_bash_output_command(
        "echo hello > report.txt", artifact_base_dir="", entity_root=str(tmp_path),
    )

    assert result.command == "echo hello > report.txt"
    assert result.output_paths == ()


def test_preserves_non_output_shell_syntax_when_no_workspace_scope(tmp_path):
    command = "echo '>' && cat < source.txt"

    result = scope_bash_output_command(
        command, artifact_base_dir="", entity_root=str(tmp_path),
    )

    assert result.command == command
    assert result.output_paths == ()


def test_scopes_only_copy_destination(tmp_path):
    result = scope_bash_output_command(
        "cp source.txt output.txt",
        artifact_base_dir="Workspaces/_by_id/folder/tasks/task",
        entity_root=str(tmp_path),
    )

    assert "source.txt" in result.command
    assert result.output_paths == ("Workspaces/_by_id/folder/tasks/task/artifacts/output.txt",)


def test_leaves_unrecognized_program_output_operands_to_the_save_back_check(tmp_path):
    """Only redirections and local file-command destinations are rewritten; a
    program's own output option is checked when its file is saved back."""
    result = scope_bash_output_command(
        "curl -o image.png https://example.test/image.png",
        artifact_base_dir="Workspaces/_by_id/folder/tasks/task",
        entity_root=str(tmp_path),
    )

    assert result.command == "curl -o image.png https://example.test/image.png"
    assert result.output_paths == ()


def test_preserves_quoted_input_and_read_redirection(tmp_path):
    command = "cat 'input report.txt' < source.txt > report.txt"
    result = scope_bash_output_command(
        command,
        artifact_base_dir="Workspaces/_by_id/folder/tasks/task",
        entity_root=str(tmp_path),
    )

    assert "'input report.txt' < source.txt >" in result.command
    assert result.output_paths == ("Workspaces/_by_id/folder/tasks/task/artifacts/report.txt",)


def test_keeps_existing_and_canonical_output_paths(tmp_path):
    (tmp_path / "existing.txt").write_text("old", encoding="utf-8")
    for target in (
        "existing.txt",
        "Workspaces/_by_id/other/tasks/old.txt",
    ):
        result = scope_bash_output_command(
            f"touch {target}",
            artifact_base_dir="Workspaces/_by_id/folder/tasks/task",
            entity_root=str(tmp_path),
        )
        assert result.output_paths == (target,)


def test_sandbox_scratch_and_working_copy_targets(tmp_path):
    """Bash runs in the user's shared sandbox from the agent's working copy:
    /tmp, /dev/null and the shared /workspace tmpfs are scratch; an absolute
    path into a working copy is an entity path."""

    base = "Workspaces/_by_id/folder/tasks/task"
    for command in (
        "ls Knowledge 2>/dev/null", "echo hi > /tmp/out.txt", "touch /tmp/x", "cp a.txt /tmp/a.txt",
        "echo hi > /workspace/report.txt", "echo hi > /workspace/shared/report.txt",
    ):
        result = scope_bash_output_command(command, artifact_base_dir=base, entity_root=str(tmp_path))
        assert result.output_paths == (), command
    aliased = scope_bash_output_command(
        "echo hi > /workspace/c/conv-1/agents/main/workspace/report.txt",
        artifact_base_dir=base, entity_root=str(tmp_path),
    )
    assert aliased.output_paths == (f"{base}/artifacts/report.txt",)


def test_fails_closed_for_ambiguous_output_forms(tmp_path):
    for command in (
        "cp -t folder source.txt",
        "echo hello > ../escape.txt",
        "echo hello > $OUTPUT",
        "echo hello > `name.txt`",
        "echo hello > {report,copy}.txt",
        "echo hello > ~/report.txt",
        "touch report.txt > log.txt",
        "mkdir report > log.txt",
        "echo hello > $MANOR_OUTPUT_DIR/$NAME.txt",
        "echo $(python a.py 2>&1) > out.txt",
        "ls && echo $(python a.py 2>&1) > out.txt",
    ):
        try:
            scope_bash_output_command(
                command,
                artifact_base_dir="Workspaces/_by_id/folder/tasks/task",
                entity_root=str(tmp_path),
            )
        except BashOutputPathError:
            pass
        else:
            raise AssertionError(f"Expected an error for {command}")


def test_keeps_equals_in_ordinary_file_name(tmp_path):
    result = scope_bash_output_command(
        "touch draft=final.txt",
        artifact_base_dir="Workspaces/_by_id/folder/tasks/task",
        entity_root=str(tmp_path),
    )

    assert result.output_paths == (
        "Workspaces/_by_id/folder/tasks/task/artifacts/draft=final.txt",
    )


def test_numeric_echo_argument_is_not_a_descriptor(tmp_path):
    result = scope_bash_output_command(
        "echo 2026 > report.txt", artifact_base_dir="Workspaces/_by_id/F", entity_root=str(tmp_path),
    )
    assert result.command == "echo 2026 > Workspaces/_by_id/F/report.txt"
    result = scope_bash_output_command(
        "echo '2026'> report.txt", artifact_base_dir="Workspaces/_by_id/F", entity_root=str(tmp_path),
    )
    assert result.command == "echo '2026'> Workspaces/_by_id/F/report.txt"


def test_escaped_filename_and_comment_keep_shell_meaning(tmp_path):
    import subprocess

    result = scope_bash_output_command(
        "touch report\\ final.txt # user comment",
        artifact_base_dir="Workspaces/_by_id/F", entity_root=str(tmp_path),
    )
    directory = tmp_path / "Workspaces/_by_id/F"
    directory.mkdir(parents=True)
    subprocess.run(result.command, cwd=tmp_path, shell=True, check=True)
    assert result.output_paths == ("Workspaces/_by_id/F/report final.txt",)
    assert [item.name for item in directory.iterdir()] == ["report final.txt"]
    assert result.command.endswith(" # user comment")


def test_copy_destination_retains_directory_operand(tmp_path):
    result = scope_bash_output_command(
        "cp source.txt output/", artifact_base_dir="Workspaces/_by_id/F", entity_root=str(tmp_path),
    )
    assert result.command == "cp source.txt Workspaces/_by_id/F/output/"


def test_every_command_of_a_list_scopes_its_own_outputs(tmp_path):
    """Each command of a list is scoped on its own.

    A line that starts with a local file command scopes every write target;
    any other line keeps its targets (checked on save-back) except an
    explicit $MANOR_OUTPUT_DIR one. Commands after a directory change keep
    their own meaning.
    """

    base = "Workspaces/_by_id/F/tasks/T"
    out = f"{base}/artifacts"
    for command, expected in (
        ("python x.py > o.txt && wc -l o.txt", None),
        ("echo a; python x.py > o.txt", f"echo a; python x.py > {out}/o.txt"),
        ("python x.py 2> err.txt >| o.txt", None),
        ('python x.py > "$OUT"', None),
        ('python x.py > "$MANOR_OUTPUT_DIR/o.txt" 2>&1', f"python x.py > {out}/o.txt 2>&1"),
        ("ffmpeg -i a.mp4 b.mp4 &> ff.log", None),
        ("cp a.txt b.txt 2>/dev/null", f"cp a.txt {out}/b.txt 2>/dev/null"),
        ("echo a > x.txt && cd sub && echo b > y.txt", f"echo a > {out}/x.txt && cd sub && echo b > y.txt"),
        ("cd Knowledge && python3 make.py > report.txt", None),
        # Only a line that starts with a local file command scopes its local
        # commands, so a program in the same line still finds their output.
        ("pip install x && mkdir -p out && python gen.py --out out/x.png", None),
        ("python a.py && echo $(ls 2>&1) > o.txt", None),
        ('echo hi > "$MANOR_OUTPUT_DIR/r.txt"', f"echo hi > {out}/r.txt"),
        ("echo hi > o.txt && cd Knowledge && ls", f"echo hi > {out}/o.txt && cd Knowledge && ls"),
        # Here-document bodies, comparisons and comments name no output.
        ("cat <<'EOF' > gen.py\nprint(1 > 0)\nEOF\npython3 gen.py", f"cat <<'EOF' > {out}/gen.py\nprint(1 > 0)\nEOF\npython3 gen.py"),
        ("[[ a > b ]] && echo $((3>2)) # > note", None),
        ("cd Knowledge && python3 make.py > /tmp/o.txt", None),
    ):
        result = scope_bash_output_command(command, artifact_base_dir=base, entity_root=str(tmp_path))
        assert result.command == (expected or command), command

    # The folder a local command made and a program's redirect into it are
    # both moved into the Workspace, never split between Workspace and root.
    mixed = scope_bash_output_command(
        "mkdir -p out && python x.py > out/y.txt", artifact_base_dir=base, entity_root=str(tmp_path),
    ).command
    assert "mkdir -p Workspaces/" in mixed and "python x.py > Workspaces/" in mixed


def test_write_resources_name_every_file_redirection_form():
    """Receipts bind what the shared lexer reports, so every operator that
    opens a file must name its target and descriptor moves must not."""
    from packages.core.ai.runtime.bash_command_specs import BashCommandSpecFactory

    for command, expected in (
        ("python x.py &> log.txt", ["log.txt"]),
        ("cmd &>> app.log", ["app.log"]),
        ("echo hi >&out.txt", ["out.txt"]),
        ("echo hi >&2; echo x 2>&1", []),
        ("cat <<EOF > g.py\nprint(1 > 0)\nEOF\npython g.py", ["g.py"]),
    ):
        assert [r.path for r in BashCommandSpecFactory.write_resources(command)] == expected, command
