import json

from packages.core.ai.tools.generate_file.schema import GENERATE_FILE_SCHEMA
from packages.core.contracts.audio_generation import (
    AudioGenerationErrorCode,
    AudioGenerationFormat,
    AudioGenerationProvider,
    AudioGenerationPurpose,
    AudioGenerationRole,
    AudioGenerationStatus,
    GenerateFileKind,
    normalize_audio_generation_purpose,
)


def test_generate_file_schema_is_derived_from_contract_enums():
    properties = GENERATE_FILE_SCHEMA["function"]["parameters"]["properties"]
    audio_properties = properties["params"]["properties"]

    assert properties["kind"]["enum"] == GenerateFileKind.values()
    assert audio_properties["purpose"]["enum"] == AudioGenerationPurpose.values()
    assert audio_properties["response_format"]["enum"] == AudioGenerationFormat.values()


def test_audio_generation_enum_values_are_json_strings():
    payload = {
        "kind": GenerateFileKind.AUDIO,
        "status": AudioGenerationStatus.ERROR,
        "code": AudioGenerationErrorCode.AUDIO_PROVIDER_UNAVAILABLE,
        "provider": AudioGenerationProvider.OPENROUTER,
        "purpose": AudioGenerationPurpose.NARRATION,
        "role": AudioGenerationRole.VOICE,
    }

    assert json.loads(json.dumps(payload)) == {
        "kind": "audio",
        "status": "error",
        "code": "audio_provider_unavailable",
        "provider": "openrouter",
        "purpose": "narration",
        "role": "voice",
    }


def test_audio_generation_purpose_aliases_normalize_to_enum_members():
    assert normalize_audio_generation_purpose("tts") is AudioGenerationPurpose.SPEECH
    assert normalize_audio_generation_purpose("score") is AudioGenerationPurpose.MUSIC
    assert normalize_audio_generation_purpose("background_bed") is AudioGenerationPurpose.AMBIENCE
    assert normalize_audio_generation_purpose("foley") is AudioGenerationPurpose.SFX
    assert normalize_audio_generation_purpose("narration") is AudioGenerationPurpose.NARRATION
