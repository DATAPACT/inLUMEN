from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from typing import Any, Protocol


DEFAULT_IMAGE_REGISTRY = "ghcr.io"


def node_image_reference(flow_id: str, configuration_hash: str, *, prefix: str) -> str:
    registry = (
        os.getenv("INLUMEN_IMAGE_REGISTRY", "").strip().rstrip("/")
        or DEFAULT_IMAGE_REGISTRY
    )
    if "://" in registry:
        raise ValueError("INLUMEN_IMAGE_REGISTRY must not include a URL scheme.")

    normalized_flow_id = re.sub(r"[^a-z0-9._-]+", "-", flow_id.lower()).strip(
        "-._"
    )
    if not normalized_flow_id:
        raise ValueError(f"{prefix} image generation requires a valid flow_id.")

    hash_value = configuration_hash.removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", hash_value):
        raise ValueError(
            f"{prefix} image generation requires a SHA-256 configuration hash."
        )
    normalized_prefix = re.sub(r"[^a-z0-9._-]+", "-", prefix.lower()).strip("-._")
    if not normalized_prefix:
        raise ValueError("Image prefix must be valid.")
    return f"{registry}/inlumen/{normalized_prefix}-{normalized_flow_id}:{hash_value[:12]}"


@dataclass(frozen=True)
class GeneratedFile:
    filename: str
    content: str
    content_type: str = "text/plain"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GeneratedRuntimeArtifacts:
    flow_id: str
    definition_id: str
    definition_version: int
    generator: str
    generator_version: str
    configuration_hash: str
    image_reference: str
    entrypoint: tuple[str, ...]
    files: tuple[GeneratedFile, ...]
    manifest: dict[str, Any]

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        files = [generated_file.to_dict() for generated_file in self.files]
        if not include_content:
            files = [
                {
                    "filename": generated_file["filename"],
                    "content_type": generated_file["content_type"],
                }
                for generated_file in files
            ]
        return {
            "flow_id": self.flow_id,
            "definition_id": self.definition_id,
            "definition_version": self.definition_version,
            "generator": self.generator,
            "generator_version": self.generator_version,
            "configuration_hash": self.configuration_hash,
            "image_reference": self.image_reference,
            "entrypoint": list(self.entrypoint),
            "data_contract": self.manifest.get("data_contract", {}),
            "files": files,
            "manifest": self.manifest,
        }

    def dockerfile_artifact(self) -> dict[str, Any]:
        dockerfile = next(
            generated_file
            for generated_file in self.files
            if generated_file.filename.startswith("Dockerfile.")
        )
        return {
            "dockerfile_filename": dockerfile.filename,
            "content": dockerfile.content,
            "flow_id": self.flow_id,
            "image": self.image_reference,
            "command": list(self.entrypoint),
            "files": self.manifest["build"]["context_files"],
            "generator": self.generator,
            "configuration_hash": self.configuration_hash,
            "build_manifest": self.manifest["build"]["manifest_filename"],
        }


class NodeGenerator(Protocol):
    name: str
    version: str

    def generate(
        self,
        step: dict[str, Any],
        graph: dict[str, Any] | None = None,
    ) -> GeneratedRuntimeArtifacts:
        ...
