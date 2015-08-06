import collections
import yaml
import inflection
import re
import os

from .config import convert_to_boolean, convert_to_number


class ContentBuilder(object):
    """An ordered set of sections each made up of one or more questions.

    Usage::

        >>> content = ContentBuilder(sections)
        >>> content.get_section_data(section_id, form_data)
        {'field': 'value', 'field2': 'value2'}
    """
    def __init__(self, sections):
        self.sections = [ContentSection(section) for section in sections]

    def __iter__(self):
        return self.sections.__iter__()

    def get_section(self, section_id):
        """Return a section by ID"""
        for section in self.sections:
            if section["id"] == section_id:
                return section
        return None

    def get_all_data(self, form_data):
        """Extract data for all sections from a submitted form

        :param form_data: the submitted form data
        :type form_data: :class:`werkzeug.ImmutableMultiDict`
        :return: parsed and filtered data

        See :func:`ContentBuilder.get_section_data` for more details.
        """
        all_data = {}
        for section in self:
            all_data.update(section.get_data(form_data))
        return all_data

    def get_next_section_id(self, section_id=None, only_editable=False):
        previous_section_is_current = section_id is None

        for section in self.sections:
            if only_editable:
                if previous_section_is_current and section.get('editable'):
                    return section["id"]
            else:
                if previous_section_is_current:
                    return section["id"]

            if section["id"] == section_id:
                previous_section_is_current = True

        return None

    def get_next_editable_section_id(self, section_id=None):
        return self.get_next_section_id(section_id, True)

    def filter(self, service_data):
        """Return a new :class:`ContentBuilder` filtered by service data

        Only includes the questions that should be shown for the provided
        service data. This is calculated by resolving the dependencies
        described by the `depends` section."""
        sections = filter(None, [
            self._get_section_filtered_by(section, service_data)
            for section in self.sections
        ])

        return ContentBuilder(sections)

    def _get_section_filtered_by(self, section, service_data):
        section = section.copy()

        filtered_questions = [
            question for question in section["questions"]
            if self._question_should_be_shown(
                question.get("depends"), service_data
            )
        ]

        if len(filtered_questions):
            section.section["questions"] = filtered_questions
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

    def get_question(self, question_id):
        for section in self:
            question = section.get_question(question_id)
            if question:
                return question


class ContentSection(collections.Mapping):
    def __init__(self, section):
        if isinstance(section, ContentSection):
            section = section.section
        self.section = section

    def __getitem__(self, key):
        return self.section[key]

    def __iter__(self):
        return self.section.__iter__()

    def __len__(self):
        return len(self.section)

    def copy(self):
        return ContentSection(self.section.copy())

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
        section_data = {}
        for key in set(form_data) & set(self._get_fields()):
            if self._is_list_type(key):
                section_data[key] = form_data.getlist(key)
            elif self._is_boolean_type(key):
                section_data[key] = convert_to_boolean(form_data[key])
            elif self._is_numeric_type(key):
                section_data[key] = convert_to_number(form_data[key])
            elif self._is_pricing_type(key):
                section_data.update(expand_pricing_field(form_data.getlist(key)))
            elif self._is_not_upload(key):
                section_data[key] = form_data[key]

            if self._has_assurance(key):
                section_data[key] = {
                    "value": section_data[key],
                    "assurance": form_data.get(key + '--assurance'),
                }
        return section_data

    def _get_fields(self):
        return [q['id'] for q in self['questions']]

    def get_question(self, question_id):
        """Return a question dictionary by question ID"""
        # TODO: investigate how this would work as get by form field name
        for question in self.section['questions']:
            if question['id'] == question_id:
                return question

    # Type checking

    def _is_type(self, key, *types):
        """Return True if a given key is one of the provided types"""
        return self.get_question(key)['type'] in types

    def _is_list_type(self, key):
        """Return True if a given key is a list type"""
        return key == 'serviceTypes' or self._is_type(key, 'list', 'checkboxes')

    def _is_not_upload(self, key):
        """Return True if a given key is not a file upload"""
        return not self._is_type(key, 'upload')

    def _is_boolean_type(self, key):
        """Return True if a given key is a boolean type"""
        return self._is_type(key, 'boolean')

    def _is_numeric_type(self, key):
        """Return True if a given key is a numeric type"""
        return self._is_type(key, 'percentage')

    def _is_pricing_type(self, key):
        """Return True if a given key is a pricing type"""
        return self._is_type(key, 'pricing')

    def _has_assurance(self, key):
        """Return True if a question has an assurance component"""
        return self.get_question(key).get('assuranceApproach', False)


class ContentLoader(object):
    def __init__(self, manifest, content_directory):
        manifest_sections = read_yaml(manifest)

        self._questions = {
            q: _load_question(q, content_directory)
            for section in manifest_sections
            for q in section["questions"]
        }

        self._sections = [
            self._populate_section(s) for s in manifest_sections
        ]

    def get_question(self, question):
        q = self._questions.get(question, {}).copy()
        if q:
            q['id'] = question
        return q

    def get_builder(self):
        return ContentBuilder(self._sections)

    def _populate_section(self, section):
        section["id"] = _make_section_id(section["name"])
        section["questions"] = [
            self.get_question(q) for q in section["questions"]
        ]

        return section

# TODO: move this into question definition with questions represented by multiple fields
PRICE_FIELDS = ['priceMin', 'priceMax', 'priceUnit', 'priceInterval']


def expand_pricing_field(pricing):
    if len(pricing) < len(PRICE_FIELDS):
        raise ValueError("The pricing field did not have enough elements: {}".format(pricing))
    return {
        field_name: pricing[i] for i, field_name in enumerate(PRICE_FIELDS)
        if len(pricing[i]) > 0
    }


def _load_question(question, directory):
    question_content = read_yaml(
        directory + question + ".yml"
    )
    question_content["id"] = _make_question_id(question)

    return question_content


def _make_section_id(name):
    return inflection.underscore(
        re.sub(r"\s", "_", name)
    )


def _make_question_id(question):
    if re.match('^serviceTypes(SCS|SaaS|PaaS|IaaS)', question):
        return 'serviceTypes'
    return question


def read_yaml(yaml_file):
    if not os.path.isfile(yaml_file):
        return {}
    with open(yaml_file, "r") as file:
        return yaml.load(file)
