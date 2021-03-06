# SPDX-License-Identifier: Apache-2.0
#
# http://nexb.com and https://github.com/nexB/scancode.io
# The ScanCode.io software is licensed under the Apache License version 2.0.
# Data generated with ScanCode.io is provided as-is without warranties.
# ScanCode is a trademark of nexB Inc.
#
# You may not use this software except in compliance with the License.
# You may obtain a copy of the License at: http://apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software distributed
# under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.
#
# Data Generated with ScanCode.io is provided on an "AS IS" BASIS, WITHOUT WARRANTIES
# OR CONDITIONS OF ANY KIND, either express or implied. No content created from
# ScanCode.io should be considered or used as legal advice. Consult an Attorney
# for any legal advice.
#
# ScanCode.io is a free software code scanning tool from nexB Inc. and others.
# Visit https://github.com/nexB/scancode.io for support and download.

import re
import shutil
import traceback
import uuid
from contextlib import suppress
from pathlib import Path

from django.core import checks
from django.core.exceptions import ObjectDoesNotExist
from django.db import models
from django.forms import model_to_dict
from django.utils.translation import gettext_lazy as _

from packageurl import normalize_qualifiers

from scancodeio import WORKSPACE_LOCATION
from scanner.models import AbstractTaskFieldsModel
from scanpipe import tasks
from scanpipe.packagedb_models import AbstractPackage
from scanpipe.packagedb_models import AbstractResource
from scanpipe.pipelines import get_pipeline_doc


def get_project_work_directory(project):
    """
    Return the work directory location for the provided `project`.
    """
    return f"{WORKSPACE_LOCATION}/projects/{project.name}-{project.short_uuid}"


class UUIDPKModel(models.Model):
    uuid = models.UUIDField(
        verbose_name=_("UUID"),
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        db_index=True,
    )

    class Meta:
        abstract = True

    def __str__(self):
        return str(self.uuid)

    @property
    def short_uuid(self):
        return str(self.uuid)[0:8]


class Project(UUIDPKModel, models.Model):
    """
    The Project encapsulate all analysis processing.
    Multiple analysis pipelines can be run on the project.
    """

    created_date = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        help_text=_("Creation date for this project."),
    )
    name = models.CharField(
        unique=True,
        db_index=True,
        max_length=100,
        help_text=_("Name for this project."),
    )
    WORK_DIRECTORIES = ["input", "output", "codebase", "tmp"]
    work_directory = models.CharField(
        max_length=2048,
        editable=False,
        help_text=_("Project work directory location."),
    )
    extra_data = models.JSONField(default=dict, editable=False)

    class Meta:
        ordering = ["-created_date"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.work_directory:
            self.work_directory = get_project_work_directory(self)
            self.setup_work_directory()
        super().save(*args, **kwargs)

    def setup_work_directory(self):
        """
        Create all the work_directory structure, skip existing.
        """
        for subdirectory in self.WORK_DIRECTORIES:
            Path(self.work_directory, subdirectory).mkdir(parents=True, exist_ok=True)

    @property
    def work_path(self):
        return Path(self.work_directory)

    @property
    def input_path(self):
        return Path(self.work_path / "input")

    @property
    def output_path(self):
        return Path(self.work_path / "output")

    @property
    def codebase_path(self):
        return Path(self.work_path / "codebase")

    @property
    def tmp_path(self):
        return Path(self.work_path / "tmp")

    def clear_tmp_directory(self):
        """
        Delete the whole tmp/ directory content.
        This is call at the end of each Pipelines Run, do not store content
        that is needed for further processing in following Pipelines.
        """
        shutil.rmtree(self.tmp_path, ignore_errors=True)
        self.tmp_path.mkdir(exist_ok=True)

    def inputs(self, pattern="**/*"):
        """
        Return a generator of all the files and directories path of the input/
        directory matching the provided `pattern`.
        The default "**" pattern means: "this directory and all subdirectories,
        recursively".
        """
        return self.input_path.glob(pattern)

    @property
    def input_files(self):
        """
        Return the list of all files relative path in the input/ directory,
        recursively.
        """
        return [
            str(path.relative_to(self.input_path))
            for path in self.inputs()
            if path.is_file()
        ]

    @property
    def input_root(self):
        """
        Return the list of all files and directories of the input/ directory.
        Only the first level children are listed.
        """
        return [
            str(path.relative_to(self.input_path)) for path in self.input_path.glob("*")
        ]

    def add_input_file(self, file_object):
        filename = file_object.name
        file_path = Path(self.input_path / filename)

        with open(file_path, "wb+") as f:
            for chunk in file_object.chunks():
                f.write(chunk)

    def add_pipeline(self, pipeline):
        description = get_pipeline_doc(pipeline)
        return Run.objects.create(
            project=self, pipeline=pipeline, description=description
        )

    def get_next_run(self):
        with suppress(ObjectDoesNotExist):
            return self.runs.filter(task_id__isnull=True).earliest("created_date")


class ProjectRelatedQuerySet(models.QuerySet):
    def project(self, project):
        return self.filter(project=project)


class ProjectRelatedModel(models.Model):
    """
    Base model for all models that are related to a Project.
    """

    project = models.ForeignKey(
        Project, related_name="%(class)ss", on_delete=models.CASCADE, editable=False
    )

    objects = models.Manager.from_queryset(ProjectRelatedQuerySet)()

    class Meta:
        abstract = True

    @classmethod
    def model_fields(cls):
        return [field.name for field in cls._meta.get_fields()]


class ProjectError(UUIDPKModel, ProjectRelatedModel):
    """
    Store errors and exceptions raised during a pipeline run.
    """

    created_date = models.DateTimeField(auto_now_add=True, editable=False)
    model = models.CharField(max_length=100, help_text=_("Name of the model class."))
    details = models.JSONField(
        default=dict, blank=True, help_text=_("Data that caused the error.")
    )
    message = models.TextField(blank=True, help_text=_("Error message."))
    traceback = models.TextField(blank=True, help_text=_("Exception traceback."))

    class Meta:
        ordering = ["created_date"]


class SaveProjectErrorMixin:
    """
    Use `SaveProjectErrorMixin` on a model to create a ProjectError entry
    from a raised exception during `save()` in place of stopping the analysis
    process.
    """

    def save(self, *args, **kwargs):
        try:
            super().save(*args, **kwargs)
        except Exception as e:
            ProjectError.objects.create(
                project=self.project,
                model=self.__class__.__name__,
                details=model_to_dict(self),
                message=str(e),
                traceback="".join(traceback.format_tb(e.__traceback__)),
            )

    @classmethod
    def check(cls, **kwargs):
        errors = super().check(**kwargs)
        errors += [*cls._check_project_field(**kwargs)]
        return errors

    @classmethod
    def _check_project_field(cls, **kwargs):
        """
        Check if `project` field is defined.
        """

        fields = [f.name for f in cls._meta.local_fields]
        if "project" not in fields:
            return [
                checks.Error(
                    "'project' field is required when using SaveProjectErrorMixin.",
                    obj=cls,
                    id="scanpipe.models.E001",
                )
            ]

        return []


class Run(UUIDPKModel, ProjectRelatedModel, AbstractTaskFieldsModel):
    pipeline = models.CharField(max_length=1024)
    created_date = models.DateTimeField(auto_now_add=True, db_index=True)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["created_date"]

    def __str__(self):
        return f"{self.pipeline}"

    def run_pipeline_task_async(self):
        tasks.run_pipeline_task.apply_async(args=[self.pk], queue="default")

    def resume_pipeline_task_async(self):
        tasks.resume_pipeline_task.apply_async(args=[self.pk], queue="default")

    @property
    def task_succeeded(self):
        return self.task_exitcode == 0

    def get_run_id(self):
        """
        Return the run id from the task output.
        """
        if self.task_output:
            run_id_re = re.compile(r"run-id [0-9]+")
            run_id_string = run_id_re.search(self.task_output).group()
            if run_id_string:
                return run_id_string.split()[-1]


class CodebaseResourceQuerySet(ProjectRelatedQuerySet):
    def status(self, status=None):
        if status:
            return self.filter(status=status)

        return self.exclude(status="")

    def no_status(self):
        return self.filter(status="")


class ScanFieldsModelMixin(models.Model):
    """
    Fields returned by ScanCode-toolkit scans.
    """

    copyrights = models.JSONField(
        blank=True,
        default=list,
        help_text=_(
            "List of detected copyright statements (and related detection details)."
        ),
    )
    holders = models.JSONField(
        blank=True,
        default=list,
        help_text=_(
            "List of detected copyright holders (and related detection details)."
        ),
    )
    authors = models.JSONField(
        blank=True,
        default=list,
        help_text=_("List of detected authors (and related detection details)."),
    )
    licenses = models.JSONField(
        blank=True,
        default=list,
        help_text=_("List of license detection details."),
    )
    license_expressions = models.JSONField(
        blank=True,
        default=list,
        help_text=_("List of detected license expressions."),
    )
    emails = models.JSONField(
        blank=True,
        default=list,
        help_text=_("List of detected emails (and related detection details)."),
    )
    urls = models.JSONField(
        blank=True,
        default=list,
        help_text=_("List of detected URLs (and related detection details)."),
    )

    class Meta:
        abstract = True


class CodebaseResource(
    ProjectRelatedModel, ScanFieldsModelMixin, SaveProjectErrorMixin, AbstractResource
):
    rootfs_path = models.CharField(
        max_length=2000,
        blank=True,
        help_text=_(
            "Path relative to some root filesystem root directory. "
            "Useful when working on disk images, docker images, and VM images."
            'Eg.: "/usr/bin/bash" for a path of "tarball-extract/rootfs/usr/bin/bash"'
        ),
    )
    status = models.CharField(
        blank=True,
        max_length=30,
        help_text=_("Analysis status for this resource."),
    )

    class Type(models.TextChoices):
        FILE = "file"
        DIRECTORY = "directory"
        SYMLINK = "symlink"

    type = models.CharField(
        max_length=10,
        choices=Type.choices,
        help_text=_(
            "Type of this resource as one of: {}".format(", ".join(Type.values))
        ),
    )
    extra_data = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Optional mapping of extra data key/values."),
    )
    name = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("File or directory name of this resource."),
    )
    extension = models.CharField(
        max_length=100,
        blank=True,
        help_text=_(
            "File extension for this resource (directories do not have an extension)."
        ),
    )
    programming_language = models.CharField(
        max_length=50,
        blank=True,
        help_text=_("Programming language of this resource if this is a code file."),
    )
    mime_type = models.CharField(
        max_length=100,
        blank=True,
        help_text=_(
            "MIME type (aka. media type) for this resource. "
            "See https://en.wikipedia.org/wiki/Media_type"
        ),
    )
    file_type = models.CharField(
        max_length=1024,
        blank=True,
        help_text=_("Descriptive file type for this resource."),
    )

    objects = models.Manager.from_queryset(CodebaseResourceQuerySet)()

    class Meta:
        unique_together = (("project", "path"),)
        ordering = ("project", "path")

    def __str__(self):
        return self.path

    @property
    def location(self):
        # strip the leading / to allow joining this with the codebase_path
        path = Path(str(self.path).strip("/"))
        return str(self.project.codebase_path / path)

    @property
    def file_content(self):
        """
        Return the content of this Resource file using TextCode utilities for
        optimal compatibility.
        """
        from textcode.analysis import numbered_text_lines

        numbered_lines = numbered_text_lines(self.location)
        return "".join(l for _, l in numbered_lines)

    @property
    def for_packages(self):
        return [str(package) for package in self.discovered_packages.all()]

    def set_scan_results(self, scan_results, save=False):
        model_fields = self.model_fields()
        for field_name, value in scan_results.items():
            if value and field_name in model_fields:
                setattr(self, field_name, value)

        if save:
            self.save()


class DiscoveredPackage(ProjectRelatedModel, SaveProjectErrorMixin, AbstractPackage):
    codebase_resources = models.ManyToManyField(
        "CodebaseResource", related_name="discovered_packages"
    )
    missing_resources = models.JSONField(default=list, blank=True)
    modified_resources = models.JSONField(default=list, blank=True)

    # AbstractPackage overrides:
    keywords = models.JSONField(default=list, blank=True)
    source_packages = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["uuid"]

    def __str__(self):
        return self.package_url or str(self.uuid)

    @classmethod
    def create_from_data(cls, project, package_data):
        """
        Create and return a DiscoveredPackage for `project` using the
        `package_data` mapping.
        # TODO: we should ensure these entries are UNIQUE
        # tomd: Create a ProjectError if not unique?
        """
        qualifiers = package_data.get("qualifiers")
        if qualifiers:
            package_data["qualifiers"] = normalize_qualifiers(qualifiers, encode=True)

        cleaned_package_data = {
            field_name: value
            for field_name, value in package_data.items()
            if field_name in DiscoveredPackage.model_fields() and value
        }

        return cls.objects.create(project=project, **cleaned_package_data)

    @classmethod
    def create_for_resource(cls, package_data, codebase_resource):
        """
        Create a DiscoveredPackage instance using the `package_data` and assign
        it to the provided `codebase_resource`.
        """
        project = codebase_resource.project
        created_package = cls.create_from_data(project, package_data)
        codebase_resource.discovered_packages.add(created_package)
        return created_package
