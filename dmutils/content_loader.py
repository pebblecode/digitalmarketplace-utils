import yaml
import inflection
import re
import os
from collections import defaultdict
from functools import partial
from werkzeug.datastructures import ImmutableMultiDict

from .config import convert_to_boolean, convert_to_number


class ContentNotFoundError(Exception):
    pass


class ContentManifest(object):
    """An ordered set of sections each made up of one or more questions.

    Example::
        # Get hold of a section
        content = ContentManifest(sections)
        section = content.get_section(section_id)

        # Extract data from form data
        section.get_data(form_data)
    """
    def __init__(self, sections):
        self.sections = [ContentSection.create(section) for section in sections]
        question_index = 0
        for section in self.sections:
            for question in section.questions:
                question_index += 1
                question.number = question_index

    def __iter__(self):
        return self.sections.__iter__()

    def summary(self, service_data):
        """Create a manifest instance for service summary display

        Return a new :class:`ContentManifest` instance with all
        questions replaced with :class:`ContentQuestionSummary`.

        :class:`ContentQuestionSummary` instances in addition to
        question data contain a reference to the service data
        dictionary and so have additional properties used by the
        summary tables.

        """
        return ContentManifest(
            [section.summary(service_data) for section in self.sections]
        )

    def get_section(self, section_id):
        """Return a section by ID"""
        for section in self.sections:
            if section.id == section_id:
                return section
        return None

    def get_all_data(self, form_data):
        """Extract data for all sections from a submitted form

        :param form_data: the submitted form data
        :type form_data: :class:`werkzeug.ImmutableMultiDict`
        :return: parsed and filtered data

        See :func:`SectionContent.get_data` for more details.
        """
        all_data = {}
        for section in self.sections:
            all_data.update(section.get_data(form_data))
        return all_data

    def get_next_section_id(self, section_id=None, only_editable=False):
        previous_section_is_current = section_id is None

        for section in self.sections:
            if only_editable:
                if previous_section_is_current and section.editable:
                    return section.id
            else:
                if previous_section_is_current:
                    return section.id

            if section.id == section_id:
                previous_section_is_current = True

        return None

    def get_next_editable_section_id(self, section_id=None):
        return self.get_next_section_id(section_id, True)

    def filter(self, service_data):
        """Return a new :class:`ContentManifest` filtered by service data

        Only includes the questions that should be shown for the provided
        service data. This is calculated by resolving the dependencies
        described by the `depends` section."""
        sections = filter(None, [
            self._get_section_filtered_by(section, service_data)
            for section in self.sections
        ])

        return ContentManifest(sections)

    def _get_section_filtered_by(self, section, service_data):
        section = section.copy()

        filtered_questions = [
            question for question in section.questions
            if self._question_should_be_shown(
                question.get("depends"), service_data
            )
        ]

        if len(filtered_questions):
            section.questions = filtered_questions
            return section
        else:
            return None

    def _question_should_be_shown(self, dependencies, service_data):
        if dependencies is None:
            return True
        for depends in dependencies:
            if not depends["on"] in service_data:
                return False
            if not service_data[depends["on"]] in depends["being"]:
                return False
        return True

    def get_question(self, field_name):
        for section in self.sections:
            question = section.get_question(field_name)
            if question:
                return question


class ContentSection(object):
    @classmethod
    def create(cls, section):
        if isinstance(section, ContentSection):
            return section.copy()
        else:
            return ContentSection(
                slug=section['slug'],
                name=section['name'],
                editable=section.get('editable'),
                edit_questions=section.get('edit_questions'),
                questions=[ContentQuestion(question) for question in section['questions']],
                description=section.get('description'))

    def __init__(self, slug, name, editable, edit_questions, questions, description=None):
        self.id = slug  # TODO deprecated, use `.slug` instead
        self.slug = slug
        self.name = name
        self.editable = editable
        self.edit_questions = edit_questions
        self.questions = questions
        self.description = description

    def __getitem__(self, key):
        return getattr(self, key)

    def copy(self):
        return ContentSection(
            slug=self.slug,
            name=self.name,
            editable=self.editable,
            edit_questions=self.edit_questions,
            questions=self.questions[:],
            description=self.description)

    def summary(self, service_data):
        summary_section = self.copy()
        summary_section.questions = [question.summary(service_data) for question in summary_section.questions]

        return summary_section

    def get_question_as_section(self, question_slug):
        question = self.get_question_by_slug(question_slug)
        if not question:
            return None
        return ContentSection(
            slug=question.slug,
            name=question.label,
            editable=self.edit_questions,
            edit_questions=False,
            questions=question.questions,
            description=question.get('description')
        )

    def get_field_names(self):
        """Return a list of field names that this section returns

        This list of field names corresponds to the keys of the data returned
        by :func:`ContentSection.get_data`. This only affects the pricing question
        that gets expanded into the the pricing fields.
        """

        return [
            form_field for question in self.questions for form_field in question.form_fields
        ]

    def get_question_ids(self, type=None):
        return [
            form_field for question in self.questions for form_field in question.get_question_ids(type)
        ]

    def get_data(self, form_data):
        """Extract data for a section from a submitted form

        :param form_data: the submitted form data
        :type form_data: :class:`werkzeug.ImmutableMultiDict`
        :return: parsed and filtered data

        This parses the provided form data against the expected fields for this
        section. Any fields provided in the form data that are not described
        in the section are dropped. Any fields in the section that are not
        in the form data are ignored. Fields in the form data are parsed according
        to their type in the section data.
        """
        # strip trailing and leading whitespace from form values
        form_data = ImmutableMultiDict((k, v.strip()) for k, v in form_data.items(multi=True))

        section_data = {}
        for question in self.questions:
            section_data.update(question.get_data(form_data))

        return section_data

    def has_changes_to_save(self, service, update_data):
        """Test whether an update includes changes to save

        :param service: the service that is to be updated
        :param update_data: the update that is going to be applied
        :return: whether there are changes that need saving

        If there are any keys in the update data that have different values
        the service data then a save is required.

        If there any questions in this section that are not yet present in
        the service data then a save is required (to generate the appropriate
        validation error from the API).
        """
        return any([
            any(service.get(key) != update_data[key] for key in update_data),
            any(question.id not in service for question in self.questions)
        ])

    def get_error_messages(self, errors, lot):
        """Convert API error keys into error messages

        :param errors: error dictionary as returned by the data API
        :param lot: the lot of the service
        :return: error dictionary with human readable error messages
        """
        errors_map = {}
        for question_id, message_key in errors.items():
            field_name = question_id
            if question_id == 'serviceTypes':
                field_name = question_id = 'serviceType{}'.format(lot)
            elif question_id in PRICE_FIELDS:
                message_key = self._rewrite_pricing_error_key(question_id, message_key)
                field_name = question_id = 'priceString'
            elif message_key == 'assurance_required':
                field_name = '{}--assurance'.format(question_id)

            validation_message = self.get_error_message(question_id, message_key)

            errors_map[field_name] = {
                'input_name': field_name,
                'question': self.get_question(question_id)['question'],
                'message': validation_message,
            }
        return errors_map

    def get_error_message(self, question_id, message_key):
        """Return a single error message

        :param question_id:
        :param message_key: error message key as returned by the data API
        """
        for validation in self.get_question(question_id)['validations']:
            if validation['name'] == message_key:
                return validation['message']
        return 'There was a problem with the answer to this question'

    def _rewrite_pricing_error_key(self, question_id, message_key):
        """Return a rewritten error message_key for a pricing error"""
        if message_key == 'answer_required':
            if question_id == 'priceMin':
                return 'no_min_price_specified'
            elif question_id == 'priceUnit':
                return 'no_unit_specified'
        elif message_key == 'not_money_format':
            if question_id == 'priceMin':
                return 'min_price_not_a_number'
            elif question_id == 'priceMax':
                return 'max_price_not_a_number'
        return message_key

    def unformat_data(self, data):
        """Unpack assurance information to be used in a form

        :param data: the service data as returned from the data API
        :type data: dict
        :return: service data with unpacked assurance

        Unpack fields from service JSON that have assurance information. In the
        data API response the field would be::

            {"field": {"assurance": "some assurance", "value": "some value"}}

        This then gets unpacked into two fields::

            {"field": "some value", "field--assurance": "some assurance"}
        """
        result = {}
        for key in data:
            if self._has_assurance(key):
                result[key + '--assurance'] = data[key].get('assurance', None)
                result[key] = data[key].get('value', None)
            else:
                result[key] = data[key]
        return result

    def get_question(self, field_name):
        """Return a question dictionary by question ID"""

        for question in self.questions:
            field_question = question.get_question(field_name)
            if field_question:
                return field_question

    def get_question_by_slug(self, question_slug):
        for question in self.questions:
            if question.get('slug') == question_slug:
                return question

    # Type checking

    def _has_assurance(self, key):
        """Return True if a question has an assurance component"""
        question = self.get_question(key)
        return bool(question) and question.get('assuranceApproach', False)


class ContentQuestion(object):
    def __init__(self, data, number=None):
        self.number = number
        self._data = data.copy()

        if 'questions' in data:
            self.questions = [
                question if isinstance(question, ContentQuestion) else ContentQuestion(question)
                for question in data['questions']
            ]
        else:
            self.questions = None

    def summary(self, service_data):
        return ContentQuestionSummary(
            self, service_data
        )

    def get_question(self, field_name):
        if self.id == field_name:
            return self
        elif self.questions:
            return next(
                (question for question in self.questions if question.id == field_name),
                None
            )

    def get_data(self, form_data):
        if self.id in form_data and self.type == 'pricing':
            return expand_pricing_field(form_data.getlist(self.id))
        elif self.questions:
            questions_data = {}
            for question in self.questions:
                questions_data.update(question.get_data(form_data))
            return questions_data
        else:
            return self._get_single_question_data(form_data)

    def _get_single_question_data(self, form_data):
        if self.id not in form_data:
            if self.get('assuranceApproach') and '{}--assurance'.format(self.id) in form_data:
                return {self.id: {'assurance': form_data.get('{}--assurance'.format(self.id))}}
            else:
                return {}

        if self.id == 'serviceTypes' or self.type in ['list', 'checkboxes']:
            value = form_data.getlist(self.id)
        elif self.type == 'boolean':
            value = convert_to_boolean(form_data[self.id])
        elif self.type == 'percentage':
            value = convert_to_number(form_data[self.id])
        elif self.type != 'upload':
            value = form_data[self.id]
        else:
            return {}

        if self.get('assuranceApproach'):
            value = {
                "value": value,
                "assurance": form_data.get(self.id + '--assurance'),
            }

        return {self.id: value}

    @property
    def label(self):
        return self.get('name') or self.question

    @property
    def form_fields(self):
        if self.type == 'pricing':
            return PRICE_FIELDS
        else:
            return self.get_question_ids()

    def get_question_ids(self, type=None):
        if self.questions:
            return [question.id for question in self.questions if type in [question.type, None]]
        else:
            return [self.id] if type in [self.type, None] else []

    def get(self, key, default=None):
        try:
            return getattr(self, key)
        except AttributeError:
            return default

    def __getattr__(self, key):
        try:
            return self._data[key]
        except KeyError:
            raise AttributeError(key)

    def __getitem__(self, key):
        return getattr(self, key)

    def __repr__(self):
        return '<ContentQuestion: number={0.number}, data={0._data}>'.format(self)


class ContentQuestionSummary(ContentQuestion):
    def __init__(self, question, service_data):
        self.number = question.number
        self._data = question._data
        self._service_data = service_data

        self.questions = question.questions
        if self.questions:
            self.questions = [q.summary(service_data) for q in self.questions]

    @property
    def value(self):
        if self.questions:
            return self.questions
        return self._service_data.get(self.id, '')

    @property
    def answer_required(self):
        if self.questions:
            return any(question.answer_required for question in self.questions)
        if self.value in ['', [], None]:
            if not self.get('optional'):
                return True
        else:
            return False


class ContentLoader(object):
    """Load the frameworks content files

    Usage:
    >>> loader = ContentLoader('path/to/digitalmarketplace-frameworks')
    >>> # pre-load manifests
    >>> loader.load_manifest('framework-1', 'question-set-1', 'manifest-1')
    >>> loader.load_manifest('framework-1', 'question-set-1', 'manifest-2')
    >>> loader.load_manifest('framework-1', 'question-set-2', 'manifest-3')
    >>> loader.load_manifest('framework-2', 'question-set-1', 'manifest-1')
    >>>
    >>> # preload messages
    >>> loader.load_messages('framework-1', ['homepage_sidebar', 'dashboard'])
    >>>
    >>> # get a manifest
    >>> loader.get_manifest('framework-1', 'manifest-1')
    >>>
    >>> # get a message
    >>> loader.get_message('framework-1', 'homepage_sidebar', 'in_review')

    """
    def __init__(self, content_path):
        self.content_path = content_path
        self._content = defaultdict(dict)
        self._messages = defaultdict(dict)
        # A defaultdict that defaults to a defaultdict of dicts
        self._questions = defaultdict(partial(defaultdict, dict))

    def get_manifest(self, framework_slug, manifest):
        try:
            if framework_slug not in self._content:
                raise KeyError
            manifest = self._content[framework_slug][manifest]
        except KeyError:
            raise ContentNotFoundError("Content not found for {} and {}".format(framework_slug, manifest))

        return ContentManifest(manifest)

    get_builder = get_manifest  # TODO remove once apps have switched to .get_manifest

    def load_manifest(self, framework_slug, question_set, manifest):
        if manifest not in self._content[framework_slug]:
            try:
                manifest_path = self._manifest_path(framework_slug, manifest)
                manifest_sections = read_yaml(manifest_path)
            except IOError:
                raise ContentNotFoundError("No manifest at {}".format(manifest_path))

            self._content[framework_slug][manifest] = [
                self._load_nested_questions(framework_slug, question_set, section) for section in manifest_sections
            ]

        return self._content[framework_slug][manifest]

    def _has_question(self, framework_slug, question_set, question):
        if framework_slug not in self._questions:
            return False
        if question_set not in self._questions[framework_slug]:
            return False

        return question in self._questions[framework_slug][question_set]

    def get_question(self, framework_slug, question_set, question):
        if not self._has_question(framework_slug, question_set, question):
            try:
                questions_path = self._questions_path(framework_slug, question_set)
                self._questions[framework_slug][question_set][question] = self._load_nested_questions(
                    framework_slug, question_set,
                    _load_question(question, questions_path)
                )
            except IOError:
                raise ContentNotFoundError("No question {} at {}".format(question, questions_path))

        return self._questions[framework_slug][question_set][question].copy()

    def get_message(self, framework_slug, block, framework_status, supplier_status=None):
        if block not in self._messages[framework_slug]:
            raise ContentNotFoundError(
                "Message file at {} not loaded".format(self._message_path(framework_slug, block))
            )

        return self._messages[framework_slug][block].get(
            self._message_key(framework_status, supplier_status), None
        )

    def load_messages(self, framework_slug, blocks):
        if not isinstance(blocks, list):
            raise TypeError('Content blocks must be a list')

        for block in blocks:
            try:
                self._messages[framework_slug][block] = read_yaml(
                    self._message_path(framework_slug, block)
                )
            except IOError:
                raise ContentNotFoundError(
                    "No message file at {}".format(self._message_path(framework_slug, block))
                )

    def _root_path(self, framework_slug):
        return os.path.join(self.content_path, 'frameworks', framework_slug)

    def _questions_path(self, framework_slug, question_set):
        return os.path.join(self._root_path(framework_slug), 'questions', question_set)

    def _manifest_path(self, framework_slug, manifest):
        return os.path.join(self._root_path(framework_slug), 'manifests', '{}.yml'.format(manifest))

    def _message_path(self, framework_slug, message):
        return os.path.join(self._root_path(framework_slug), 'messages', '{}.yml'.format(message))

    def _load_nested_questions(self, framework_slug, question_set, section_or_question):
        if 'questions' in section_or_question:
            section_or_question['questions'] = [
                self.get_question(framework_slug, question_set, question)
                for question in section_or_question['questions']
            ]
            section_or_question['slug'] = _make_slug(section_or_question['name'])

        return section_or_question

    def _message_key(self, framework_status, supplier_status):
        return '{}{}'.format(
            framework_status,
            '-{}'.format(supplier_status) if supplier_status else ''
        )


# TODO: move this into question definition with questions represented by multiple fields
PRICE_FIELDS = ['priceMin', 'priceMax', 'priceUnit', 'priceInterval']


def expand_pricing_field(pricing):
    if len(pricing) < len(PRICE_FIELDS):
        raise ValueError("The pricing field did not have enough elements: {}".format(pricing))
    return {
        field_name: pricing[i] for i, field_name in enumerate(PRICE_FIELDS)
    }


def _load_question(question, directory):
    question_content = read_yaml(
        os.path.join(directory, '{}.yml'.format(question))
    )

    question_content["id"] = _make_question_id(question)

    return question_content


def _make_slug(name):
    return inflection.underscore(
        re.sub(r"\s", "_", name)
    ).replace('_', '-')


def _make_question_id(question):
    if re.match('^serviceTypes(SCS|SaaS|PaaS|IaaS)', question):
        return 'serviceTypes'
    return question


def read_yaml(yaml_file):
    with open(yaml_file, "r") as file:
        return yaml.load(file)
