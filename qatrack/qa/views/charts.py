import collections
import itertools
import json
import textwrap

from django.conf import settings
from django.db.models import Count, Q
from django.http import HttpResponse
from django.template import Context
from django.template.loader import get_template
from django.utils import timezone
from django.views.generic import TemplateView, View

from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas
from matplotlib.figure import Figure
import numpy

from .. import models
from qatrack.qa.control_chart import control_chart
from qatrack.units.models import Unit
from qatrack.qa.utils import SetEncoder
from braces.views import JSONResponseMixin, PermissionRequiredMixin


JSON_CONTENT_TYPE = "application/json"


local_tz = timezone.get_current_timezone()


def get_test_lists_for_unit_frequencies(request):

    units = request.GET.getlist("units[]") or Unit.objects.values_list("pk", flat=True)
    frequencies = request.GET.getlist("frequencies[]") or models.Frequency.objects.values_list("pk", flat=True)

    fq = Q(frequency__in=frequencies)
    if '0' in frequencies:
        fq |= Q(frequency=None)

    test_lists = models.UnitTestCollection.objects.filter(
        fq,
        unit__in=units,
        content_type__name="test list"
    ).values_list("testlist__pk", flat=True)

    test_list_cycle_lists = models.UnitTestCollection.objects.filter(
        fq,
        unit__in=units,
        content_type__name="test list cycle"
    ).values_list("testlistcycle__test_lists__pk", flat=True)

    test_lists = set(test_lists) | set(test_list_cycle_lists)

    json_context = json.dumps({"test_lists": list(test_lists)})

    return HttpResponse(json_context, content_type=JSON_CONTENT_TYPE)


def get_tests_for_test_lists(request):

    test_lists = request.GET.getlist("test_lists[]") or models.TestList.objects.values_list("pk", flat=True)

    tests = []
    for pk in test_lists:
        tl = models.TestList.objects.get(pk=pk)
        tests.extend([t.pk for t in tl.ordered_tests() if t.chart_visibility])

    json_context = json.dumps({"tests": tests})
    return HttpResponse(json_context, content_type=JSON_CONTENT_TYPE)


#============================================================================
class ChartView(PermissionRequiredMixin, TemplateView):
    """View responsible for rendering the main charts user interface."""

    permission_required = "qa.can_view_charts"
    raise_exception = True

    template_name = "qa/charts.html"

    #----------------------------------------------------------------------
    def get_context_data(self, **kwargs):
        """
        Add all relevent filters to context. test_data contains all the
        tests grouped by test list/unit/frequency and is dumped as a json
        object for use on client side.
        """

        context = super(ChartView, self).get_context_data(**kwargs)

        self.set_test_lists()
        self.set_tests()
        self.set_unit_frequencies()

        now = timezone.now().astimezone(timezone.get_current_timezone()).date()

        c = {
            "from_date": now - timezone.timedelta(days=365),
            "to_date": now + timezone.timedelta(days=1),
            "frequencies": models.Frequency.objects.all(),
            "tests": self.tests,
            "test_lists": self.test_lists,
            "categories": models.Category.objects.all(),
            "statuses": models.TestInstanceStatus.objects.all(),
            "units": Unit.objects.values("pk", "name"),
            "unit_frequencies": json.dumps(self.unit_frequencies, cls=SetEncoder)
        }
        context.update(c)
        return context

    #----------------------------------------------------------------------
    def set_unit_frequencies(self):

        unit_frequencies = models.UnitTestCollection.objects.exclude(
            last_instance=None
        ).values_list(
            "unit", "frequency"
        ).order_by("unit").distinct()

        self.unit_frequencies = collections.defaultdict(set)
        for u, f in unit_frequencies:
            f = f or 0 # use 0 id for ad hoc frequencies
            self.unit_frequencies[u].add(f)

    #----------------------------------------------------------------------
    def set_test_lists(self):
        """self.test_lists is set to all test lists that have been completed
        one or more times"""

        self.test_lists = models.TestList.objects.annotate(
            instance_count=Count("testlistinstance")
        ).filter(
            instance_count__gt=0
        ).order_by(
            "name"
        ).values(
            "pk", "description", "name",
        )

    #----------------------------------------------------------------------
    def set_tests(self):
        """self.tests is set to all tests that are chartable"""

        self.tests = models.Test.objects.order_by(
            "name"
        ).filter(
            chart_visibility=True
        ).values(
            "pk", "category", "name", "description",
        )


#============================================================================
class BaseChartView(View):
    """
    Base AJAX view responsible for retrieving & tabulating data to be
    plotted for charts.
    """

    #----------------------------------------------------------------------
    def get(self, request):

        self.get_plot_data()
        headers, rows = self.create_data_table()
        resp = self.render_to_response({"data": self.plot_data, "headers": headers, "rows": rows})
        return resp

    #----------------------------------------------------------------------
    def create_data_table(self):
        """
        Take all the :model:`qa.TestInstance`s and tabulate them (grouped by
        unit/test) along with the reference value at the time they were performed.
        """

        headers = []
        max_len = 0
        cols = []

        r = lambda ref: ref if ref is not None else ""

        # collect all data in 'date/value/ref triplets
        for name, points in self.plot_data.iteritems():
            headers.append(name)
            col = [(p["display_date"], p["display"], r(p["orig_reference"])) for p in points]
            cols.append(col)
            max_len = max(len(col), max_len)

        #generate table from triplets
        rows = []
        for idx in range(max_len):
            row = []
            for col in cols:
                try:
                    row.append(col[idx])
                except IndexError:
                    row.append(["", "", ""])
            rows.append(row)

        return headers, rows

    #----------------------------------------------------------------------
    def render_table(self, headers, rows):

        context = Context({
            "ncols": 3 * len(rows[0]) if rows else 0,
            "rows": rows,
            "headers": headers
        })
        template = get_template("qa/qa_data_table.html")

        return template.render(context)

    #----------------------------------------------------------------------
    def get_date(self, key, default):
        """take date from GET data and convert it to utc"""

        #datetime strings coming in will be in local time, make sure they get
        #converted to utc

        try:
            d = timezone.datetime.strptime(self.request.GET.get(key), settings.SIMPLE_DATE_FORMAT)
        except:
            d = default

        if timezone.is_naive(d):
            d = timezone.make_aware(d, timezone.get_current_timezone())

        return d.astimezone(timezone.utc)

    #---------------------------------------------------------------
    def convert_date(self, date):
        """by default we assume date is being used by javascript, so convert to ISO"""
        return date.isoformat()

    #---------------------------------------------------------------
    def test_instance_to_point(self, ti, relative=False):
        """Grab relevent plot data from a :model:`qa.TestInstance`"""

        if relative and ti.reference:

            ref_is_not_zero = ti.reference.value != 0.
            has_percent_tol = (ti.tolerance and ti.tolerance.type == models.PERCENT)
            has_no_tol = ti.tolerance is None

            use_percent = has_percent_tol or (has_no_tol and ref_is_not_zero)

            if use_percent:
                value = 100 * (ti.value - ti.reference.value) / ti.reference.value
                ref_value = 0.
            else:
                value = ti.value - ti.reference.value
                ref_value = 0
        else:
            value = ti.value
            ref_value = ti.reference.value if ti.reference is not None else None

        point = {
            "act_high": None, "act_low": None, "tol_low": None, "tol_high": None,
            "date": self.convert_date(timezone.make_naive(ti.work_completed, local_tz)),
            "display_date": ti.work_completed,
            "value": value,
            "display": ti.value_display(),
            "reference": ref_value,
            "orig_reference": ti.reference.value if ti.reference else None,

        }

        if ti.tolerance is not None and ref_value is not None:
            if relative and ti.reference and ti.reference.value != 0. and not ti.tolerance.type == models.ABSOLUTE:
                tols = ti.tolerance.tolerances_for_value(100)
                for k in tols:
                    tols[k] -= 100.
            else:
                tols = ti.tolerance.tolerances_for_value(ref_value)

            point.update(tols)

        return point

    #----------------------------------------------------------------------
    def get_plot_data(self):
        """Retrieve all :model:`qa.TestInstance` data requested."""

        self.plot_data = {}

        now = timezone.now()
        from_date = self.get_date("from_date", now - timezone.timedelta(days=365))
        to_date = self.get_date("to_date", now)
        combine_data = self.request.GET.get("combine_data") == "true"
        relative = self.request.GET.get("relative") == "true"

        tests = self.request.GET.getlist("tests[]", [])
        test_lists = self.request.GET.getlist("test_lists[]", [])
        units = self.request.GET.getlist("units[]", [])
        statuses = self.request.GET.getlist("statuses[]", [])

        if not (tests and test_lists and units and statuses):
            return

        tests = models.Test.objects.filter(pk__in=tests)
        test_lists = models.TestList.objects.filter(pk__in=test_lists)
        units = Unit.objects.filter(pk__in=units)
        statuses = models.TestInstanceStatus.objects.filter(pk__in=statuses)

        if not combine_data:
            # retrieve test instances for every possible permutation of the
            # requested test list, test & units
            for tl, t, u in itertools.product(test_lists, tests, units):
                tis = models.TestInstance.objects.filter(
                    test_list_instance__test_list=tl,
                    unit_test_info__test=t,
                    unit_test_info__unit=u,
                    status__pk__in=statuses,
                    work_completed__gte=from_date,
                    work_completed__lte=to_date,
                    skipped=False,
                ).select_related(
                    "reference", "tolerance", "unit_test_info__test", "unit_test_info__unit", "status",
                ).order_by(
                    "work_completed"
                )
                if tis:
                    name = "%s - %s :: %s%s" % (u.name, tl.name, t.name,  " (relative to ref)" if relative else "")
                    self.plot_data[name] = [self.test_instance_to_point(ti, relative=relative) for ti in tis]
        else:
            # retrieve test instances for every possible permutation of the
            # requested test & units
            for t, u in itertools.product(tests, units):
                tis = models.TestInstance.objects.filter(
                    unit_test_info__test=t,
                    unit_test_info__unit=u,
                    status__pk__in=statuses,
                    work_completed__gte=from_date,
                    work_completed__lte=to_date,
                    skipped=False,
                ).select_related(
                    "reference", "tolerance", "unit_test_info__test", "unit_test_info__unit", "status",
                ).order_by(
                    "work_completed"
                )
                if tis:
                    name = "%s :: %s%s" % (u.name, t.name, " (relative to ref)" if relative else "")
                    self.plot_data[name] = [self.test_instance_to_point(ti, relative=relative) for ti in tis]

    #---------------------------------------------------------------------------
    def render_to_response(self, context):
        context['table'] = self.render_table(context['headers'], context['rows'])
        return self.render_json_response(context)


#============================================================================
class BasicChartData(PermissionRequiredMixin, JSONResponseMixin, BaseChartView):
    """JSON view used for basic chart type"""

    permission_required = "qa.can_view_charts"
    raise_exception = True


#============================================================================
class ControlChartImage(PermissionRequiredMixin, BaseChartView):
    """Return a control chart image from given qa data"""

    permission_required = "qa.can_view_charts"
    raise_exception = True

    #---------------------------------------------------------------------------
    def convert_date(self, dt):
        """date is being used by Python code, so no need to convert to ISO"""
        return dt

    #----------------------------------------------------------------------
    def get_number_from_request(self, param, default, dtype=float):
        """look for a number in GET and convert it to the given datatype"""
        try:
            v = dtype(self.request.GET.get(param, default))
        except:
            v = default
        return v

    #---------------------------------------------------------------
    def get_plot_data(self):
        """
        The control chart software can only handle one test at a time
        so if user requested more than one test, just grab
        one of them.
        """

        super(ControlChartImage, self).get_plot_data()

        if self.plot_data:
            self.plot_data = dict([self.plot_data.popitem()])

    #----------------------------------------------------------------------
    def render_to_response(self, context):
        """Create a png image and write the control chart image to it"""

        fig = Figure(dpi=72, facecolor="white")
        dpi = fig.get_dpi()
        fig.set_size_inches(
            self.get_number_from_request("width", 700) / dpi,
            self.get_number_from_request("height", 480) / dpi,
        )
        canvas = FigureCanvas(fig)
        dates, data = [], []

        if context["data"] and context["data"].values():
            name, points = context["data"].items()[0]
            dates, data = zip(*[(ti["date"], ti["value"]) for ti in points])

        n_baseline_subgroups = self.get_number_from_request("n_baseline_subgroups", 2, dtype=int)
        n_baseline_subgroups = max(2, n_baseline_subgroups)

        subgroup_size = self.get_number_from_request("subgroup_size", 2, dtype=int)
        if not (1 < subgroup_size < 100):
            subgroup_size = 1

        include_fit = self.request.GET.get("fit_data", "") == "true"

        response = HttpResponse(mimetype="image/png")
        if n_baseline_subgroups < 1 or n_baseline_subgroups > len(data) / subgroup_size:
            fig.text(0.1, 0.9, "Not enough data for control chart", fontsize=20)
            canvas.print_png(response)
        else:
            try:
                control_chart.display(fig, numpy.array(data), subgroup_size, n_baseline_subgroups, fit=include_fit, dates=dates)
                fig.autofmt_xdate()
                canvas.print_png(response)
            except (RuntimeError, OverflowError) as e:  # pragma: nocover
                fig.clf()
                msg = "There was a problem generating your control chart:\n%s" % str(e)
                fig.text(0.1, 0.9, "\n".join(textwrap.wrap(msg, 40)), fontsize=12)
                canvas.print_png(response)

        return response


class ExportCSVView(PermissionRequiredMixin, JSONResponseMixin, BaseChartView):
    """JSON view used for basic chart type"""

    permission_required = "qa.can_view_charts"
    raise_exception = True

    def render_to_response(self, context):
        import csv
        from django.utils import formats
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="qatrackexport.csv"'

        writer = csv.writer(response)
        header1 = []
        header2 = []
        for h in context['headers']:
            header1.extend([h.encode('utf-8'), '', ''])
            header2.extend(["Date", "Value", "Ref"])

        writer.writerow(header1)
        writer.writerow(header2)

        for row_set in context['rows']:
            row = []
            for date, val, ref in row_set:
                date = formats.date_format(date, "DATETIME_FORMAT") if date is not "" else ""
                row.extend([date, val, ref])
            writer.writerow(row)

        return response
