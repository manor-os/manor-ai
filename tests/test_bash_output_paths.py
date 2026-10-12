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


def test_keeps_ephemeral_sandbox_output_operand(tmp_path):
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
        str(tmp_path / "existing.txt"),
        "Workspaces/_by_id/other/tasks/old.txt",
    ):
        result = scope_bash_output_command(
            f"touch {target}",
            artifact_base_dir="Workspaces/_by_id/folder/tasks/task",
            entity_root=str(tmp_path),
        )
        assert result.output_paths == (target,)


def test_creates_only_scoped_output_ancestors(tmp_path):
    from packages.core.ai.tools.bash_tool import _ensure_bash_output_directories

    base = "Workspaces/_by_id/folder/tasks/task"
    _ensure_bash_output_directories(
        entity_root=str(tmp_path),
        artifact_base_dir=base,
        output_paths=(f"{base}/nested/report.txt", "existing.txt"),
    )

    assert (tmp_path / base / "nested").is_dir()
    assert not (tmp_path / "existing.txt").exists()


def test_fails_closed_for_ambiguous_output_forms(tmp_path):
    for command in (
        "cp -t folder source.txt",
        "echo hello 2> error.txt",
        "echo hello >| forced.txt",
        "echo hello > ../escape.txt",
        "echo hello > $OUTPUT",
        "echo hello > `name.txt`",
        "echo hello > {report,copy}.txt",
        "echo hello > ~/report.txt",
        "touch report.txt > log.txt",
        "mkdir report > log.txt",
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
