"""
  Base classes go here.
"""
import datetime
import operator
import re

from testplan.common.utils.convert import nested_groups
from testplan.common.utils.timing import utcnow
from testplan.common.utils.table import TableEntry

from testplan import defaults


__all__ = [
    'BaseEntry',
    'Group',
    'Summary',
    'Log',
]


# Will be used for default conversion like: NotEqual -> Not Equal
ENTRY_NAME_PATTERN = re.compile(r"([A-Z])")

DEFAULT_CATEGORY = 'DEFAULT'


def readable_name(class_name):
    """NotEqual -> Not Equal"""
    return ENTRY_NAME_PATTERN.sub(' \\1', class_name).strip()


def get_table(source, keep_column_order=True):
    """
    Return table formatted as a TableEntry.

    :param source: Tabular data.
    :type source: ``list`` of ``list`` or ``list`` of ``dict``
    :param keep_column_order: Flag whether column order should be maintained.
    :type keep_column_order: ``bool``
    :return: Formatted table.
    :rtype: ``list`` of ``dict``
    """
    if not source:
        return []

    if not isinstance(source, TableEntry):
        table = TableEntry(source)
    return table.as_list_of_dict(keep_column_order=keep_column_order)


class BaseEntry(object):
    """Base class for all entries, stores common context like time etc."""

    meta_type = 'entry'

    def __init__(self, description, category=None):
        self.utc_time = utcnow()
        self.machine_time = datetime.datetime.now()
        self.description = description
        self.category = category or DEFAULT_CATEGORY

        # Will be set explicitly via containers
        self.line_no = None
        self.file_path = None

    def __str__(self):
        return repr(self)

    def __bool__(self):
        return True

    @property
    def name(self):
        """MyClass -> My Class"""
        return readable_name(self.__class__.__name__)

    __nonzero__ = __bool__


class Group(object):

    # we treat Groups as assertions so we can render them with pass/fail context
    meta_type = 'assertion'

    def __init__(self, entries, description=None):
        self.description = description
        self.entries = entries

    def __bool__(self):
        return self.passed

    __nonzero__ = __bool__

    def __repr__(self):
        return "{}(entries={}, description='{}')".format(
            self.__class__.__name__,
            self.entries,
            self.description
        )

    @property
    def passed(self):
        """
        Empty groups are truthy AKA does not
        contain anything that is failing.
        """
        return (not self.entries) or all([bool(e) for e in self.entries])


class Summary(Group):
    """
    A meta assertion that stores a subset of given entries.
    Groups assertion data into a nested structure by category, assertion type
    and pass/fail status.

    If any of the entries is a Group, then its entries are expanded and
    the Group object is discarded.
    """

    def __init__(
        self, entries, description=None,
        num_passing=defaults.SUMMARY_NUM_PASSING,
        num_failing=defaults.SUMMARY_NUM_FAILING
    ):
        self.num_passing = num_passing
        self.num_failing = num_failing

        super(Summary, self).__init__(
            entries=self._summarize(
                entries,
                num_passing=num_passing,
                num_failing=num_failing
            ),
            description=description)

    def _flatten(self, entries):
        """
        Recursively traverse entries and expand entries of groups.
        """
        def _flatten(items):
            result = []
            for item in items:
                if isinstance(item, Group) and not isinstance(item, Summary):
                    result.extend(_flatten(item.entries))
                else:
                    result.append(item)
            return result
        return _flatten(entries)

    def _summarize(self, entries, num_passing, num_failing):
        # Circular imports
        from .assertions import Assertion
        from .summarization import registry

        # Get rid of Groups (but leave summaries)
        entries = self._flatten(entries)
        summaries = [e for e in entries if isinstance(e, Summary)]

        # Create nested data of depth 3
        # Group by category, class name and pass/fail status
        groups = nested_groups(
            iterable=(
                e for e in entries
                if isinstance(e, Assertion)
            ),
            key_funcs=[
                operator.attrgetter('category'),
                lambda obj: obj.__class__.__name__,
                operator.truth
            ]
        )

        result = []

        for category, category_grouping in groups:
            cat_group = Group(
                entries=[],
                description='Category: {}'.format(category)
            )
            for class_name, assertion_grouping in category_grouping:
                asr_group = Group(
                    entries=[],
                    description='Assertion type: {}'.format(readable_name(class_name))
                )
                for pass_status, assertion_entries in assertion_grouping:
                    # Apply custom grouping, otherwise just trim the
                    # list of entries via default summarization func.
                    summarizer = registry[class_name]
                    summary_group = summarizer(
                        category=category,
                        class_name=class_name,
                        passed=pass_status,
                        entries=assertion_entries,
                        limit=num_passing if pass_status else num_failing,
                    )
                    if len(summary_group.entries):
                        asr_group.entries.append(summary_group)
                cat_group.entries.append(asr_group)
            result.append(cat_group)
        return summaries + result


class Log(BaseEntry):

    def __init__(self, message):
        super(Log, self).__init__(description=message)

    def __str__(self):
        return self.description


class MatPlot(BaseEntry):
    """Display a Matplotlib graph in the report."""
    def __init__(self, pyplot, image_file_path, width=2, height=2,
                 description=None):
        dpi = 96
        self.width = float(width)
        self.height = float(height)
        self.image_file_path = image_file_path
        pyplot.savefig(image_file_path, dpi=dpi, pad_inches=0, transparent=True)
        pyplot.close()

        super(MatPlot, self).__init__(description=description)


class TableLog(BaseEntry):
    """Log a table to the report."""
    def __init__(self, table, display_index=False, description=None):
        self.table = get_table(table)
        self.indices = range(len(self.table))
        self.display_index = display_index
        self.columns = self.table[0].keys()

        super(TableLog, self).__init__(description=description)