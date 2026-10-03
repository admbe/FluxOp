"""Executive workbook export.

One governed .xlsx for a business audience: the fiscal-year outlook with
budget-group tracking on the same sheet, the service composition in
resource/economic terms, commitment posture, and the stated assumptions.
Everything in it comes from the same governed reads the Reports page
uses -- the workbook is a presentation of the system of record, never a
separate calculation. Operational warnings (backfill progress,
administrator attention) stay in the app; the workbook states the
measured spend impact of a data gap instead, and omits it entirely when
the impact is immaterial.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.chart.axis import ChartLines
from openpyxl.chart.series import SeriesLabel
from openpyxl.chart.text import RichText
from openpyxl.drawing.text import (
    CharacterProperties,
    Paragraph,
    ParagraphProperties,
    RichTextProperties,
)
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.pagebreak import Break
from openpyxl.worksheet.properties import PageSetupProperties
from openpyxl.worksheet.worksheet import Worksheet

from .database import COVERAGE_ADMIN_LIMITATION_PREFIX

_TITLE_FONT = Font(name="Aptos Display", size=22, bold=True, color="10211C")
_SECTION_FONT = Font(name="Aptos", size=10, bold=True, color="087F5B")
_SECTION_TITLE_FONT = Font(name="Aptos Display", size=15, bold=True, color="10211C")
_SUBTITLE_FONT = Font(name="Aptos", size=10, color="687973")
_HEADER_FONT = Font(name="Aptos", bold=True, color="FFFFFF")
_HEADER_FILL = PatternFill("solid", fgColor="12372D")
_WARN_FILL = PatternFill("solid", fgColor="FDE9D9")
_OVER_FILL = PatternFill("solid", fgColor="F8D7DA")
_UNDER_FILL = PatternFill("solid", fgColor="D6F0E0")
_INFO_FILL = PatternFill("solid", fgColor="E8F1FA")
_SUBTLE_FILL = PatternFill("solid", fgColor="F4F7F5")
_MONEY = "$#,##0"

# Below this share of the relevant projected monthly run rate, a data gap
# is presentational noise for a business audience: the workbook omits it
# (2026-08-11 report feedback). At or above it, the workbook states the
# measured dollar impact instead of the operational warning.
_MATERIAL_IMPACT_SHARE = 0.01
# A real percent format, not '0.0"%"'. The latter appends a literal percent
# sign without scaling, so a fraction rendered through it lost two orders of
# magnitude: a budget group running 49.5% over displayed as "0.5%". Both call
# sites already pass fractions (variance/annualBudget, percentOfTotal/100),
# so Excel's own x100 is exactly what they need.
_PERCENT = "0.0%"


def _show_axes(chart: Any, category_title: str, value_title: str) -> None:
    """Force axis lines, tick labels and titles onto an exported chart.

    openpyxl leaves ``delete`` and ``tickLblPos`` unset, and both Excel and
    LibreOffice render that as a plot with no scale on either axis -- which
    is exactly how the FY-outlook chart shipped (2026-08-10 report
    feedback: "missing any bearing on X and Y axis"). Every chart in this
    workbook goes through here so a new sheet cannot regress it.
    """
    chart.x_axis.delete = False
    chart.y_axis.delete = False
    chart.x_axis.title = category_title
    chart.y_axis.title = value_title
    chart.x_axis.tickLblPos = "low"
    chart.y_axis.tickLblPos = "nextTo"
    chart.y_axis.majorGridlines = ChartLines()


def _axis_label_text(size: int = 900, rotation_degrees: int = 0) -> RichText:
    """Compact, optionally rotated tick labels.

    openpyxl leaves tick-label text properties unset, so twelve monthly
    categories rendered at the default size collide with each other and
    with the legend (2026-08-11 report feedback: "overlapping or
    difficult-to-read text"). Rotated labels keep their full text at any
    plot width.
    """
    properties = CharacterProperties(sz=size)
    return RichText(
        bodyPr=RichTextProperties(
            rot=rotation_degrees * 60000, vert="horz"
        ),
        p=[
            Paragraph(
                pPr=ParagraphProperties(defRPr=properties),
                endParaRPr=properties,
            )
        ],
    )


def _material_impact(measured_monthly: float, projected_monthly: float) -> bool:
    """A data gap is stated only when its measured spend is material."""
    if not measured_monthly or measured_monthly <= 0:
        return False
    if not projected_monthly or projected_monthly <= 0:
        return True
    return (measured_monthly / projected_monthly) >= _MATERIAL_IMPACT_SHARE


def _sheet_title(sheet: Worksheet, title: str, subtitle: str) -> int:
    """Use the report's restrained executive hierarchy on every sheet."""
    sheet["A2"] = title
    sheet["A2"].font = _TITLE_FONT
    sheet["A3"] = subtitle
    sheet["A3"].font = Font(name="Aptos", size=10, color="687973")
    sheet.merge_cells("A2:J2")
    sheet.merge_cells("A3:J3")
    sheet.sheet_view.showGridLines = False
    sheet.row_dimensions[2].height = 32
    sheet.row_dimensions[3].height = 20
    # Print one page wide, however many pages tall. Without this, every
    # sheet spilled its right-hand columns and half of each chart onto
    # overflow pages when printed or saved to PDF (2026-08-11 report
    # feedback: content "runs off the page").
    sheet.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    return 5


def _header_row(sheet: Worksheet, row: int, headers: list[str]) -> int:
    for index, header in enumerate(headers, start=1):
        cell = sheet.cell(row=row, column=index, value=header)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(vertical="center")
    sheet.row_dimensions[row].height = 22
    return row + 1


def _autosize(sheet: Worksheet, widths: dict[int, int]) -> None:
    for column, width in widths.items():
        sheet.column_dimensions[get_column_letter(column)].width = width


def _summary_sheet(
    sheet: Worksheet,
    outlook: dict[str, Any],
    commitments: dict[str, Any],
    executive: dict[str, Any],
    composition: dict[str, Any] | None,
) -> None:
    currency = outlook.get("currency") or "USD"
    row = _sheet_title(
        sheet,
        f"Azure spend — executive summary ({outlook.get('fiscalYear')})",
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        f" · cost basis {outlook.get('costType')} · currency {currency}"
        " · produced by FluxFinOps governed reporting",
    )
    spend = executive.get("spend") or {}
    anomalies = executive.get("anomalies") or {}
    savings = executive.get("savings") or {}
    coverage = outlook.get("subscriptionCoverage") or {}
    entries = [
        ("Fiscal year", outlook.get("fiscalYear"), None),
        ("FY actual to date", outlook.get("actualToDate"), _MONEY),
        ("FY projected total", outlook.get("fyTotal"), _MONEY),
        (
            "Projection range",
            f"{outlook.get('fyLower'):,.0f} – {outlook.get('fyUpper'):,.0f}",
            None,
        ),
        ("FY budget", outlook.get("fyBudget"), _MONEY),
        ("Variance vs budget", outlook.get("fyVarianceVsBudget"), _MONEY),
        ("Forecast range width", (outlook.get("fyUpper") or 0) - (outlook.get("fyLower") or 0), _MONEY),
        ("Month-to-date spend", spend.get("mtdActual"), _MONEY),
        ("Active cost anomalies", anomalies.get("count"), None),
        ("Realized savings (measured)", savings.get("realizedMonthly"), _MONEY),
        (
            "Active reservations",
            (commitments.get("summary") or {}).get("activeCount"),
            None,
        ),
        (
            "Reservations expiring ≤ 120 days",
            (commitments.get("summary") or {}).get("expiringWithin120Days"),
            None,
        ),
        (
            "Monthly-history coverage",
            f"{coverage.get('covered', 0)} of {coverage.get('configured', 0)}"
            " subscriptions",
            None,
        ),
    ]
    for label, value, number_format in entries:
        sheet.cell(row=row, column=1, value=label).font = Font(bold=True)
        cell = sheet.cell(row=row, column=2, value=value)
        if number_format and isinstance(value, (int, float)):
            cell.number_format = number_format
        if label == "Variance vs budget" and isinstance(value, (int, float)):
            cell.fill = _OVER_FILL if value > 0 else _UNDER_FILL
        if label == "Forecast range width":
            cell.fill = _INFO_FILL
        row += 1
    row += 1
    if composition:
        row = _composition_section(sheet, composition, row)
        row += 1
    sheet.cell(row=row, column=1, value="Data limitations").font = _SECTION_FONT
    row += 1
    # Operational coverage warnings stay in the app. The workbook states
    # the measured spend impact of the exclusion instead, and only when
    # that impact is material to the projection.
    limitations = [
        item
        for item in outlook.get("limitations") or []
        if not item.startswith(COVERAGE_ADMIN_LIMITATION_PREFIX)
    ]
    impact = coverage.get("excludedImpact") or {}
    measured_monthly = float(impact.get("measuredMonthlySpend") or 0.0)
    projected_monthly = (outlook.get("fyTotal") or 0.0) / 12
    if _material_impact(measured_monthly, projected_monthly):
        share = measured_monthly / projected_monthly if projected_monthly else None
        limitations.append(
            f"The outlook excludes {impact.get('count')} subscription(s)"
            " whose cost history is still being collected; their measured"
            f" spend is ~${measured_monthly:,.0f}/month"
            + (
                f" (~{share * 100:.1f}% of the projected monthly run rate)."
                if share is not None
                else "."
            )
        )
    for limitation in limitations or ["None recorded."]:
        sheet.cell(row=row, column=1, value=f"• {limitation}")
        row += 1
    _autosize(sheet, {1: 34, 2: 16})
    sheet.freeze_panes = "A4"


def _outlook_sheet(sheet: Worksheet, outlook: dict[str, Any]) -> None:
    """FY outlook and budget groups, stacked vertically on one sheet.

    The two sections were separate sheets, with each table's chart placed
    beside it; side-by-side content ran off the printed page (2026-08-11
    report feedback). Everything here now flows top to bottom in columns
    A-H.
    """
    row = _sheet_title(
        sheet,
        f"Fiscal-year outlook — {outlook.get('fiscalYear')}",
        "Actual through the current month; projected thereafter with a lower/"
        "upper planning range. Monthly budget "
        + (
            f"${outlook.get('budgetMonthly'):,.0f}."
            if outlook.get("budgetMonthly") is not None
            else "not configured."
        ),
    )
    sheet.page_setup.orientation = "landscape"
    headers = ["Month", "Status", "Spend", "Actual", "Lower", "Upper", "Monthly budget", "Variance"]
    data_start = _header_row(sheet, row, headers)
    budget = outlook.get("budgetMonthly")
    current = data_start
    for month in outlook.get("months") or []:
        sheet.cell(row=current, column=1, value=month["month"])
        status = month["status"].replace("inProgress", "in progress")
        sheet.cell(row=current, column=2, value=status)
        is_actual = month["status"] == "actual"
        for column, value in (
            (3, month["amount"]),
            (4, month["amount"] if is_actual else None),
            (5, month["lower"] if not is_actual else None),
            (6, month["upper"] if not is_actual else None),
        ):
            cell = sheet.cell(row=current, column=column, value=value)
            cell.number_format = _MONEY
        if is_actual:
            sheet.cell(row=current, column=2).font = Font(bold=True, color="087F5B")
        elif status == "in progress":
            sheet.cell(row=current, column=2).font = Font(bold=True, color="A56500")
        else:
            sheet.cell(row=current, column=2).font = Font(color="687973")
        if budget is not None:
            cell = sheet.cell(row=current, column=7, value=budget)
            cell.number_format = _MONEY
            if month["amount"] > budget:
                sheet.cell(row=current, column=3).font = Font(bold=True, color="C2410C")
            variance = month["amount"] - budget
            variance_cell = sheet.cell(row=current, column=8, value=variance)
            variance_cell.number_format = _MONEY
            variance_cell.font = Font(
                bold=True,
                color="C2410C" if variance > 0 else "087F5B",
            )
        current += 1

    total_row = current
    sheet.cell(row=total_row, column=1, value="FY TOTAL").font = Font(bold=True)
    for column, value in (
        (3, outlook.get("fyTotal")),
        (7, outlook.get("fyBudget")),
        (8, outlook.get("fyVarianceVsBudget")),
    ):
        cell = sheet.cell(row=total_row, column=column, value=value)
        cell.font = Font(bold=True, color="C2410C" if column == 8 else "10211C")
        cell.number_format = _MONEY
    for column in range(1, 9):
        sheet.cell(row=total_row, column=column).fill = _SUBTLE_FILL

    # One line per idea: spend, its planning range, and the budget. The
    # previous chart drew a second "Actual" series over the identical
    # spend values and let twelve category labels fight a side legend,
    # which is where the unreadable overlapping text came from.
    chart = LineChart()
    chart.title = "Monthly spend vs budget"
    chart.height = 11
    chart.width = 24
    chart.y_axis.numFmt = _MONEY
    _show_axes(chart, "Fiscal month", "Monthly cost ($)")
    chart.x_axis.txPr = _axis_label_text(rotation_degrees=-45)
    chart.y_axis.txPr = _axis_label_text()
    chart.legend.position = "b"
    chart.legend.overlay = False
    chart.add_data(
        Reference(
            sheet, min_col=3, max_col=3,
            min_row=data_start - 1, max_row=current - 1,
        ),
        titles_from_data=True,
    )
    chart.add_data(
        Reference(
            sheet, min_col=5, max_col=7,
            min_row=data_start - 1, max_row=current - 1,
        ),
        titles_from_data=True,
    )
    if len(chart.series) >= 4:
        chart.series[0].tx = SeriesLabel(v="Spend")
        chart.series[0].graphicalProperties.line.solidFill = "188563"
        chart.series[0].graphicalProperties.line.width = 26000
        for index, label in ((1, "Lower range"), (2, "Upper range")):
            chart.series[index].tx = SeriesLabel(v=label)
            chart.series[index].graphicalProperties.line.solidFill = "9DCDBD"
            chart.series[index].graphicalProperties.line.prstDash = "dash"
        chart.series[3].tx = SeriesLabel(v="Monthly budget")
        chart.series[3].graphicalProperties.line.solidFill = "A3ACA8"
        chart.series[3].graphicalProperties.line.prstDash = "dash"
    chart.set_categories(
        Reference(sheet, min_col=1, min_row=data_start, max_row=current - 1)
    )
    sheet.add_chart(chart, f"A{total_row + 2}")

    # ~11 cm of chart at the default 15 pt row height, plus breathing room.
    groups_top = total_row + 25
    _groups_section(sheet, outlook, groups_top)

    # Deterministic print pages: table, then the chart, then the groups.
    # Without the breaks the printer splits the floating chart wherever
    # the page happens to end.
    sheet.row_breaks.append(Break(id=total_row))
    sheet.row_breaks.append(Break(id=groups_top - 1))

    sheet.freeze_panes = "A4"
    _autosize(
        sheet, {1: 14, 2: 15, 3: 17, 4: 14, 5: 14, 6: 14, 7: 16, 8: 17}
    )


def _groups_section(
    sheet: Worksheet, outlook: dict[str, Any], row: int
) -> int:
    """Budget-group tracking, written below the FY outlook on its sheet."""
    groups = outlook.get("groups") or []
    sheet.cell(row=row, column=1, value="Budget groups").font = _SECTION_TITLE_FONT
    sheet.row_dimensions[row].height = 24
    row += 1
    subtitle = (
        "Each group tracks its member subscriptions against its own annual"
        " envelope."
    )
    if any(group.get("allocatedSavingsMonthly") for group in groups):
        subtitle += (
            " Planning assumptions are allocated to each group in"
            " proportion to its projected spend."
        )
    sheet.cell(row=row, column=1, value=subtitle).font = _SUBTITLE_FONT
    row += 1

    headers = [
        "Group", "Annual budget", "FY actual to date", "FY projected",
        "Lower", "Upper", "Variance", "Variance %",
    ]
    data_start = _header_row(sheet, row, headers)
    current = data_start
    for group in groups:
        sheet.cell(row=current, column=1, value=group["name"]).font = Font(
            bold=True
        )
        for column, key in (
            (2, "annualBudget"), (3, "actualToDate"), (4, "fyTotal"),
            (5, "fyLower"), (6, "fyUpper"), (7, "variance"),
        ):
            cell = sheet.cell(row=current, column=column, value=group[key])
            cell.number_format = _MONEY
        variance_cell = sheet.cell(row=current, column=7)
        variance_cell.fill = (
            _OVER_FILL if group["variance"] > 0 else _UNDER_FILL
        )
        percent_cell = sheet.cell(
            row=current,
            column=8,
            value=(group["variance"] / group["annualBudget"] if group["annualBudget"] else None),
        )
        percent_cell.number_format = _PERCENT
        current += 1
    if not groups:
        sheet.cell(
            row=current,
            column=1,
            value="No budget groups configured yet (Administration →"
            " Budget groups).",
        )
        return current + 1

    # A member subscription without cost history contributes nothing to
    # its group's numbers. State what that gap is measurably worth; below
    # materiality, say nothing.
    notes = []
    for group in groups:
        excluded = float(group.get("excludedMeasuredMonthly") or 0.0)
        group_monthly = float(group.get("fyTotal") or 0.0) / 12
        if _material_impact(excluded, group_monthly):
            notes.append(
                f"{group['name']} excludes ~${excluded:,.0f}/month of"
                " measured spend from subscriptions whose cost history is"
                " still being collected."
            )
    if notes:
        current += 1
        for note in notes:
            sheet.cell(row=current, column=1, value=f"• {note}").font = _SUBTITLE_FONT
            current += 1

    chart = BarChart()
    chart.type = "bar"
    chart.style = 10
    chart.title = "Annual budget vs FY projected"
    chart.height = 8
    chart.width = 20
    chart.x_axis.numFmt = _MONEY
    chart.y_axis.numFmt = _MONEY
    _show_axes(chart, "Budget group", "Amount ($)")
    chart.y_axis.txPr = _axis_label_text()
    chart.legend.position = "b"
    chart.legend.overlay = False
    labels = Reference(
        sheet, min_col=1, min_row=data_start,
        max_row=data_start + len(groups) - 1,
    )
    for column in (2, 4):
        chart.add_data(
            Reference(
                sheet, min_col=column, max_col=column,
                min_row=data_start - 1,
                max_row=data_start + len(groups) - 1,
            ),
            titles_from_data=True,
        )
    if len(chart.series) >= 2:
        chart.series[0].tx = SeriesLabel(v="Annual budget")
        chart.series[1].tx = SeriesLabel(v="FY projected")
        chart.series[0].graphicalProperties.solidFill = "A3ACA8"
        chart.series[1].graphicalProperties.solidFill = "188563"
    chart.set_categories(labels)
    sheet.add_chart(chart, f"A{current + 2}")
    return current


# Business-facing names for the governed economic categories. The
# categorizer's confidence qualifiers ("billing classified", "resource
# unresolved") are operational detail: each such slice folds into its
# closest economic bucket here, and the method note owns the caveat. The
# governed API payload keeps the full split.
_ECONOMIC_EXPORT_LABELS = {
    "VM compute": "Virtual machine compute",
    "VM compute — billing classified": "Virtual machine compute",
    "Managed disks": "Managed disk storage",
    "Blob / File storage": "Blob / file storage",
    "Storage — resource unresolved": "Blob / file storage",
    "Backup / ASR": "Backup & site recovery",
    "Network": "Networking",
    "Other / unresolved": "Other services",
}


def _composition_section(
    sheet: Worksheet, composition: dict[str, Any], row: int
) -> int:
    """Fiscal-year actual spend by category and budget group, on Summary.

    Replaces the standalone Service composition sheet (2026-08-11
    follow-up feedback): finalized FY actuals instead of the in-progress
    month, one column per budget group, one estate total, in
    resource/economic terms only.
    """
    period_end = date.fromisoformat(composition["periodEnd"])
    last_included = period_end - timedelta(days=1)
    if composition.get("completeMonths"):
        window = (
            "Fiscal-year actuals through "
            + last_included.strftime("%b %Y")
        )
    else:
        window = (
            "Fiscal year to date through "
            + last_included.strftime("%b %d")
            + " (first month in progress)"
        )
    heading = sheet.cell(
        row=row,
        column=1,
        value=f"Spend composition — {window}",
    )
    heading.font = _SECTION_FONT
    row += 1

    columns = list(composition.get("columns") or [])
    if composition.get("hasUngrouped"):
        columns.append("Ungrouped")
    headers = ["Category", *columns, "Total"]
    row = _header_row(sheet, row, headers)
    for index in range(2, len(columns) + 3):
        sheet.cell(row=row - 1, column=index).alignment = Alignment(
            vertical="center", horizontal="right"
        )
    _autosize(
        sheet, {index: 15 for index in range(3, len(columns) + 3)}
    )

    # Fold the classifier's confidence slices into their business bucket
    # before pivoting into group columns.
    merged: dict[str, dict[str, Any]] = {}
    for item in composition.get("rows") or []:
        label = _ECONOMIC_EXPORT_LABELS.get(item["name"], item["name"])
        entry = merged.setdefault(
            label, {"byGroup": {}, "ungrouped": 0.0, "total": 0.0}
        )
        for name, value in (item.get("byGroup") or {}).items():
            entry["byGroup"][name] = entry["byGroup"].get(name, 0.0) + value
        entry["ungrouped"] += item.get("ungrouped") or 0.0
        entry["total"] += item.get("total") or 0.0

    for label, entry in sorted(
        merged.items(), key=lambda pair: -pair[1]["total"]
    ):
        sheet.cell(row=row, column=1, value=label)
        for index, name in enumerate(columns, start=2):
            value = (
                entry["ungrouped"]
                if name == "Ungrouped"
                else entry["byGroup"].get(name, 0.0)
            )
            if value:
                cell = sheet.cell(row=row, column=index, value=round(value, 2))
                cell.number_format = _MONEY
        total_cell = sheet.cell(
            row=row, column=len(columns) + 2, value=round(entry["total"], 2)
        )
        total_cell.number_format = _MONEY
        row += 1

    sheet.cell(row=row, column=1, value="Total").font = Font(bold=True)
    for index, name in enumerate(columns, start=2):
        value = round(
            sum(
                (
                    entry["ungrouped"]
                    if name == "Ungrouped"
                    else entry["byGroup"].get(name, 0.0)
                )
                for entry in merged.values()
            ),
            2,
        )
        cell = sheet.cell(row=row, column=index, value=value)
        cell.font = Font(bold=True)
        cell.number_format = _MONEY
    grand_cell = sheet.cell(
        row=row, column=len(columns) + 2, value=composition.get("grandTotal")
    )
    grand_cell.font = Font(bold=True)
    grand_cell.number_format = _MONEY
    for column in range(1, len(columns) + 3):
        sheet.cell(row=row, column=column).fill = _SUBTLE_FILL
    row += 1

    note_text = (
        f"{composition.get('costType')} daily history in"
        f" {composition.get('currency')}, attributed to budget groups by"
        " subscription. Categories reflect the underlying resource —"
        " virtual-machine spend splits into compute and disk storage;"
        " charges without a current resource identity are grouped with"
        " their closest category."
    )
    coverage_start = composition.get("coverageStartsAt")
    if coverage_start and coverage_start > composition.get("periodStart", ""):
        note_text += (
            f" Daily history begins {coverage_start}; earlier fiscal-year"
            " months are not represented."
        )
    note = sheet.cell(row=row, column=1, value=note_text)
    note.font = _SUBTITLE_FONT
    note.alignment = Alignment(wrap_text=True)
    sheet.merge_cells(
        start_row=row, start_column=1, end_row=row + 1, end_column=10
    )
    return row + 2


def _unit_economics_sheet(sheet: Worksheet, report: dict[str, Any]) -> None:
    summary = report.get("summary") or {}
    row = _sheet_title(
        sheet,
        f"Unit economics — {summary.get('dimensionLabel', 'business dimension')}",
        "Actual month-to-date cost attributed to the configured business dimension.",
    )
    data_start = _header_row(sheet, row, [summary.get("dimensionLabel", "Unit"), "Resources", "Monthly cost", "% of total"])
    row = data_start
    units = report.get("units") or []
    for item in units:
        sheet.cell(row=row, column=1, value=item["name"])
        sheet.cell(row=row, column=2, value=item["resourceCount"])
        sheet.cell(row=row, column=3, value=item["monthlyCost"]).number_format = _MONEY
        sheet.cell(row=row, column=4, value=(item.get("percentOfTotal") or 0) / 100).number_format = _PERCENT
        row += 1
    if units:
        chart = BarChart()
        chart.type = "bar"
        chart.title = "Monthly cost by unit"
        chart.height = 8
        chart.width = 20
        chart.x_axis.numFmt = _MONEY
        chart.y_axis.numFmt = _MONEY
        _show_axes(chart, "Unit", "Monthly cost ($)")
        chart.add_data(Reference(sheet, min_col=3, min_row=data_start - 1, max_row=row - 1), titles_from_data=True)
        chart.series[0].tx = SeriesLabel(v="Monthly cost")
        chart.series[0].graphicalProperties.solidFill = "2E7D5B"
        chart.set_categories(Reference(sheet, min_col=1, min_row=data_start, max_row=row - 1))
        sheet.add_chart(chart, "F4")
    sheet.cell(row=row + 1, column=1, value="Unattributed cost").font = Font(bold=True)
    sheet.cell(row=row + 1, column=2, value=summary.get("unattributedCost")).number_format = _MONEY
    sheet.freeze_panes = f"A{data_start}"
    _autosize(sheet, {1: 28, 2: 12, 3: 16, 4: 13})


def _commitments_sheet(sheet: Worksheet, commitments: dict[str, Any]) -> None:
    summary = commitments.get("summary") or {}
    row = _sheet_title(
        sheet,
        "Commitments",
        f"{summary.get('activeCount', 0)} active ·"
        f" {summary.get('expiringWithin120Days', 0)} expiring within 120 days"
        f" · fleet 30-day utilization"
        f" {summary.get('averageUtilization30d', '—')}%",
    )
    sheet.page_setup.orientation = "landscape"
    headers = [
        "Name", "SKU", "Type", "Region", "Qty", "Term", "Scope",
        "Expires", "Days left", "Util 30d %",
    ]
    current = _header_row(sheet, row, headers)
    for item in commitments.get("reservations") or []:
        values = [
            item["name"], item["sku"], item["resourceType"], item["region"],
            item["quantity"], item["term"], item["scopeType"],
            item["expiryDate"], item["daysToExpiry"], item["utilization30d"],
        ]
        for column, value in enumerate(values, start=1):
            sheet.cell(row=current, column=column, value=value)
        days = item["daysToExpiry"]
        if days is not None and days <= 120:
            for column in range(1, len(headers) + 1):
                sheet.cell(row=current, column=column).fill = (
                    _OVER_FILL if days <= 30 else _WARN_FILL
                )
        current += 1
    _autosize(
        sheet,
        {1: 26, 2: 20, 3: 15, 4: 11, 5: 6, 6: 7, 7: 19, 8: 12, 9: 10, 10: 11},
    )


def _assumptions_sheet(sheet: Worksheet, outlook: dict[str, Any]) -> None:
    config = outlook.get("config") or {}
    row = _sheet_title(
        sheet,
        "Assumptions and method",
        "The projection is a planning estimate; these are its recorded"
        " inputs.",
    )
    entries = [
        ("Method", outlook.get("methodVersion")),
        (
            "Seasonal comparison (not primary)",
            (outlook.get("seasonalComparison") or {}).get("fyTotal")
            if outlook.get("seasonalComparison")
            else "—",
        ),
        ("Months of history used", outlook.get("historyMonths")),
        ("Backtest error (MAPE)", outlook.get("backtestMape")),
        (
            "Seasonal comparison YoY factor",
            (outlook.get("seasonalComparison") or {}).get("yoyFactor")
            if outlook.get("seasonalComparison")
            else "—",
        ),
        ("Fiscal year starts", f"Month {config.get('fyStartMonth')}"),
        ("Cost basis", config.get("costType")),
        ("Growth assumption (%/month)", config.get("growthPercentMonthly")),
        (
            "Right-sizing plan savings applied",
            "Yes" if outlook.get("planSavingsApplied") else "No",
        ),
        (
            "Plan savings available ($/month)",
            outlook.get("plannedSavingsMonthly"),
        ),
        ("Savings ramp (months)", config.get("savingsRampMonths")),
        ("Notes", config.get("notes") or "—"),
        ("Assumptions last saved by", config.get("updatedBy") or "—"),
        ("Assumptions last saved at", config.get("updatedAt") or "—"),
    ]
    for label, value in entries:
        sheet.cell(row=row, column=1, value=label).font = Font(bold=True)
        sheet.cell(row=row, column=2, value=value)
        row += 1

    manual = outlook.get("manualAssumptions") or {}
    listed = [
        item
        for item in (manual.get("items") or [])
        if item.get("includeInReports")
    ]
    if listed:
        row += 1
        cell = sheet.cell(
            row=row, column=1, value="MANUAL PLANNING ASSUMPTIONS"
        )
        cell.font = _SECTION_FONT
        row += 1
        for column, header in enumerate(
            ("Assumption", "Monthly effect", "Applied", "Notes"), start=1
        ):
            head = sheet.cell(row=row, column=column, value=header)
            head.font = _HEADER_FONT
            head.fill = _HEADER_FILL
        row += 1
        for item in listed:
            sign = 1 if item.get("direction") == "saving" else -1
            sheet.cell(row=row, column=1, value=item.get("label"))
            amount = sheet.cell(
                row=row,
                column=2,
                value=sign * float(item.get("monthlyAmount") or 0),
            )
            amount.number_format = _MONEY
            sheet.cell(
                row=row,
                column=3,
                value="Yes" if item.get("enabled") else "No",
            )
            sheet.cell(row=row, column=4, value=item.get("notes") or "—")
            row += 1
        total = sheet.cell(row=row, column=1, value="Net monthly effect")
        total.font = Font(bold=True)
        net = sheet.cell(row=row, column=2, value=manual.get("monthlyNet"))
        net.font = Font(bold=True)
        net.number_format = _MONEY
        sheet.cell(
            row=row,
            column=4,
            value="Savings are positive; added costs negative. Stated "
            "estimates, not governed calculations.",
        )
    _autosize(sheet, {1: 32, 2: 44, 3: 10, 4: 52})


def build_executive_workbook(database: Any) -> bytes:
    outlook = database.fiscal_year_outlook()
    commitments = database.commitment_inventory()
    executive = database.executive_summary()
    unit_economics = database.unit_economics_report()
    composition = database.fy_actual_composition_by_group()

    workbook = Workbook()
    _summary_sheet(
        workbook.active, outlook, commitments, executive, composition
    )
    workbook.active.title = "Summary"
    _outlook_sheet(workbook.create_sheet("FY outlook"), outlook)
    _commitments_sheet(workbook.create_sheet("Commitments"), commitments)
    if unit_economics.get("configured"):
        _unit_economics_sheet(workbook.create_sheet("Unit economics"), unit_economics)
    _assumptions_sheet(workbook.create_sheet("Assumptions"), outlook)

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
