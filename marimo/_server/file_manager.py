# Copyright 2024 Marimo. All rights reserved.
from __future__ import annotations

import os
import pathlib
import shutil
from typing import Any, Dict, Optional

from marimo import _loggers
from marimo._ast import codegen
from marimo._ast.app import App, InternalApp, _AppConfig
from marimo._ast.cell import CellConfig
from marimo._config.config import WidthType
from marimo._runtime.layout.layout import (
    LayoutConfig,
    read_layout_config,
    save_layout_config,
)
from marimo._server.api.status import HTTPException, HTTPStatus
from marimo._server.models.models import (
    CopyNotebookRequest,
    SaveNotebookRequest,
)
from marimo._server.utils import canonicalize_filename

LOGGER = _loggers.marimo_logger()


class AppFileManager:
    def __init__(
        self, filename: Optional[str], default_width: WidthType | None = None
    ) -> None:
        self.filename = filename
        self._default_width: WidthType | None = default_width
        self.app = self._load_app(self.path)

    @staticmethod
    def from_app(app: InternalApp) -> AppFileManager:
        manager = AppFileManager(None)
        manager.app = app
        return manager

    def reload(self) -> None:
        """Reload the app from the file."""
        prev_cell_manager = self.app.cell_manager
        self.app = self._load_app(self.path)
        self.app.cell_manager.sort_cell_ids_by_similarity(prev_cell_manager)

    def _is_same_path(self, filename: str) -> bool:
        if self.filename is None:
            return False
        return os.path.abspath(self.filename) == os.path.abspath(filename)

    def _assert_path_does_not_exist(self, filename: str) -> None:
        if os.path.exists(filename):
            raise HTTPException(
                status_code=HTTPStatus.BAD_REQUEST,
                detail="File {0} already exists".format(filename),
            )

    def _assert_path_is_the_same(self, filename: str) -> None:
        if self.filename is not None and not self._is_same_path(filename):
            raise HTTPException(
                status_code=HTTPStatus.BAD_REQUEST,
                detail="Save handler cannot rename files.",
            )

    def _create_parent_directories(self, filename: str) -> None:
        try:
            pathlib.Path(filename).parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def _create_file(
        self,
        filename: str,
        contents: str = "",
    ) -> None:
        self._create_parent_directories(filename)
        try:
            with open(filename, "w", encoding="utf-8") as f:
                f.write(contents)
        except Exception as err:
            raise HTTPException(
                status_code=HTTPStatus.SERVER_ERROR,
                detail="Failed to save file {0}".format(filename),
            ) from err

    def _rename_file(self, new_filename: str) -> None:
        assert self.filename is not None
        self._create_parent_directories(new_filename)
        try:
            os.rename(self.filename, new_filename)
        except Exception as err:
            raise HTTPException(
                status_code=HTTPStatus.SERVER_ERROR,
                detail="Failed to rename from {0} to {1}".format(
                    self.filename, new_filename
                ),
            ) from err

    def _save_file(
        self,
        filename: str,
        codes: list[str],
        names: list[str],
        configs: list[CellConfig],
        app_config: _AppConfig,
        # Whether or not to persist the app to the file system
        persist: bool,
    ) -> str:
        LOGGER.debug("Saving app to %s", filename)
        if filename.endswith(".md"):
            # TODO: Remember just proof of concept, potentially needs
            # restructuring.
            from marimo._server.export.exporter import Exporter

            contents, _ = Exporter().export_as_md(self)
        else:
            # Header might be better kept on the AppConfig side, opposed to
            # reparsing it. Also would allow for md equivalent in a field like
            # `description`.
            header_comments = codegen.get_header_comments(filename)
            # try to save the app under the name `filename`
            contents = codegen.generate_filecontents(
                codes,
                names,
                cell_configs=configs,
                config=app_config,
                header_comments=header_comments,
            )

        if persist:
            self._create_file(filename, contents)

        if self._is_unnamed():
            self.rename(filename)

        return contents

    def _load_app(self, path: Optional[str]) -> InternalApp:
        """Read the app from the file."""
        app = codegen.get_app(path)
        if app is None:
            kwargs = (
                {"width": self._default_width}
                if self._default_width is not None
                # App decides its own default width
                else {}
            )
            empty_app = InternalApp(App(**kwargs))
            empty_app.cell_manager.register_cell(
                cell_id=None,
                code="",
                config=CellConfig(),
            )
            return empty_app
        result = InternalApp(app)
        # Ensure at least one cell
        result.cell_manager.ensure_one_cell()
        return result

    def rename(self, new_filename: str) -> None:
        """Rename the file."""
        new_filename = canonicalize_filename(new_filename)

        if self._is_same_path(new_filename):
            return

        self._assert_path_does_not_exist(new_filename)

        need_save = False
        # Check if filename is not None to satisfy mypy's type checking.
        # This ensures that filename is treated as a non-optional str,
        # preventing potential type errors in subsequent code.
        if self._is_named() and self.filename is not None:
            # Force a save after rename in case filetype changed.
            need_save = self.filename[-3:] != new_filename[-3:]
            self._rename_file(new_filename)
        else:
            self._create_file(new_filename)

        self.filename = new_filename
        if need_save:
            self._save_file(
                self.filename,
                list(self.app.cell_manager.codes()),
                list(self.app.cell_manager.names()),
                list(self.app.cell_manager.configs()),
                self.app.config,
                persist=True,
            )

    def read_layout_config(self) -> Optional[LayoutConfig]:
        if self.app.config.layout_file is not None and isinstance(
            self.filename, str
        ):
            app_dir = os.path.dirname(self.filename)
            layout = read_layout_config(app_dir, self.app.config.layout_file)
            return layout

        return None

    def read_css_file(self) -> Optional[str]:
        css_file = self.app.config.css_file
        if not css_file or not self.filename:
            return None
        return read_css_file(css_file, self.filename)

    def read_html_head_file(self) -> Optional[str]:
        html_head_file = self.app.config.html_head_file
        if not html_head_file or not self.filename:
            return None
        return read_html_head_file(html_head_file, self.filename)

    @property
    def path(self) -> Optional[str]:
        if self.filename is None:
            return None
        try:
            return os.path.abspath(self.filename)
        except AttributeError:
            return None

    def save_app_config(self, config: Dict[str, Any]) -> str:
        """Save the app configuration."""
        # Update the file with the latest app config
        # TODO(akshayka): Only change the `app = marimo.App` line (at top level
        # of file), instead of overwriting the whole file.
        new_config = self.app.update_config(config)
        if self.filename is not None:
            return self._save_file(
                self.filename,
                list(self.app.cell_manager.codes()),
                list(self.app.cell_manager.names()),
                list(self.app.cell_manager.configs()),
                new_config,
                persist=True,
            )
        return ""

    def save(self, request: SaveNotebookRequest) -> str:
        """Save the current app."""
        cell_ids, codes, configs, names, filename, layout = (
            request.cell_ids,
            request.codes,
            request.configs,
            request.names,
            request.filename,
            request.layout,
        )
        filename = canonicalize_filename(filename)
        self.app.with_data(
            cell_ids=cell_ids,
            codes=codes,
            names=names,
            configs=configs,
        )

        if self._is_named() and not self._is_same_path(filename):
            raise HTTPException(
                status_code=HTTPStatus.BAD_REQUEST,
                detail="Save handler cannot rename files.",
            )

        # save layout
        if layout is not None:
            app_dir = os.path.dirname(filename)
            app_name = os.path.basename(filename)
            layout_filename = save_layout_config(
                app_dir, app_name, LayoutConfig(**layout)
            )
            self.app.update_config({"layout_file": layout_filename})
        else:
            # Remove the layout from the config
            # We don't remove the layout file from the disk to avoid
            # deleting state that the user might want to keep
            self.app.update_config({"layout_file": None})
        return self._save_file(
            filename,
            codes,
            names,
            configs,
            self.app.config,
            persist=request.persist,
        )

    def copy(self, request: CopyNotebookRequest) -> str:
        source, destination = request.source, request.destination
        shutil.copy(source, destination)
        return os.path.basename(destination)

    def to_code(self) -> str:
        """Read the contents of the unsaved file."""
        contents = codegen.generate_filecontents(
            codes=list(self.app.cell_manager.codes()),
            names=list(self.app.cell_manager.names()),
            cell_configs=list(self.app.cell_manager.configs()),
            config=self.app.config,
        )
        return contents

    def _is_unnamed(self) -> bool:
        return self.filename is None

    def _is_named(self) -> bool:
        return self.filename is not None

    def read_file(self) -> str:
        """Read the contents of the file."""
        if self.filename is None:
            raise HTTPException(
                status_code=HTTPStatus.BAD_REQUEST,
                detail="Cannot read code from an unnamed notebook",
            )
        with open(self.filename, "r", encoding="utf-8") as f:
            contents = f.read().strip()
        return contents


def read_css_file(css_file: str, filename: Optional[str]) -> Optional[str]:
    if not css_file or not filename:
        return None

    app_dir = os.path.dirname(filename)
    filepath = os.path.join(app_dir, css_file)
    if not os.path.exists(filepath):
        LOGGER.error("CSS file %s does not exist", css_file)
        return None
    try:
        with open(filepath) as f:
            return f.read()
    except OSError as e:
        LOGGER.warning(
            "Failed to open custom CSS file %s for reading: %s",
            filepath,
            str(e),
        )
        return None


def read_html_head_file(
    html_head_file: str, filename: Optional[str]
) -> Optional[str]:
    if not html_head_file or not filename:
        return None

    app_dir = os.path.dirname(filename)
    filepath = os.path.join(app_dir, html_head_file)
    if not os.path.exists(filepath):
        LOGGER.error("HTML head file %s does not exist", html_head_file)
        return None
    try:
        with open(filepath) as f:
            return f.read()
    except OSError as e:
        LOGGER.warning(
            "Failed to open HTML head file %s for reading: %s",
            filepath,
            str(e),
        )
        return None
