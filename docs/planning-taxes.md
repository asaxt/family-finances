# Household planning tax estimates

Plan models two married adults with W-2 income, pre-tax or Roth employee-plan
contributions, and retirement withdrawals. It supports joint returns and two
separate returns, with Washington and Oregon residence/work states.
The model is a deterministic planning estimate, not a tax-return calculation.

## Income, saving, and withdrawal treatment

Gross wages are entered before tax and retirement saving. Pre-tax saving reduces
ordinary taxable income, but neither pre-tax nor Roth saving reduces federal
payroll wages. Annual employee contributions are capped at the indexed 2026
base limit of $24,500; unused requested savings remain in brokerage cash flow.
Catch-up contributions, IRA contributions, and employer matches are excluded.
Opening pre-tax and Roth balances belong to individual people. Pre-tax
withdrawals enter ordinary income; all Roth withdrawals are assumed qualified.
Brokerage capital gains, dividends, and basis are not modeled.

## Federal rules

Uses 2026 ordinary-income brackets, standard deductions and age additions,
Social Security's per-worker wage cap, Medicare, and Additional Medicare.
The temporary senior deduction phases out and ends after 2028. Future indexed
amounts use the plan inflation assumption. Additional Medicare thresholds and
the temporary senior phaseout remain fixed. Payroll always follows each worker.

## Oregon and Washington

Oregon residents include all ordinary income. Nonresidents include only wages
for work physically performed in Oregon; retirement distributions are assigned
to domicile. The work percentage allocates remaining work to the residence
state. Residence and work states are constant for all years; moves are excluded.
OR-40-N deduction proration is applied before progressive brackets, not by
multiplying the full-income tax by a work percentage. Uses the 2026 updated
standard deduction ($2,910 per spouse), $263 personal exemption credit, and
federal-tax subtraction up to $8,750 joint / $4,375 separate with income phaseout.
The Oregon age addition is $1,000 per qualifying spouse. Indexed bands and
amounts grow with the plan; statutory phaseouts and top-band thresholds do not.
Low-income tax-table rounding is not reproduced.

Washington ordinary-income tax begins in 2028 at 9.9% above a shared married
$1 million deduction. Separate returns allocate the shared deduction equally.
Nonresident deductions are prorated to Washington income. Biennial indexing
begins with 2029 income (collected in 2030), using assumed inflation. This
implements a planning approximation of the enacted law, not future DOR forms.

Resident credits for tax paid to the other state are estimated using the smaller
of each state's proportional tax on overlapping ordinary income. The estimate
uses income ratios, not every jurisdiction's precise credit worksheet. Credits
are shown separately and already deducted from the displayed state tax amounts.

## Separate-return allocation

Users must choose among reporting each person's own income, equally dividing
net wages while keeping retirement withdrawals with the owner, or equally
dividing both wages and taxable retirement withdrawals. Washington community
wages generally split equally. IRA distributions belong to their owner;
employer retirement distributions can have community and separate portions.
Mixed property ownership is not modeled. Community allocation requires a shared
domicile in this preview. These choices are scenario assumptions, not advice
about eligibility or the correct treatment of a particular household's assets.

## Spending, privacy, and limitations

Users can remove net income-tax payments already included in the 12-month
spending estimate before the model adds its calculated tax. Withholding absent
from transactions must not be removed. Calculating never changes financial
records. Explicit saving uses the existing encrypted settings table, without
schema changes or browser storage; the development mirror blocks saving.

Excludes itemization, dependents, most credits, AMT, self-employment, Social
Security benefits, required distributions, withdrawal penalties, state paid
leave, WA Cares, Oregon transit, and local taxes. Both spouses remain alive until
the younger reaches 95; age differences greater than 25 years are rejected.
Future tax indexing is an assumption, not a forecast of legal changes.

## Official sources checked September 25, 2026

- [IRS 2026 brackets and deductions](https://www.irs.gov/irb/2025-45_IRB)
- [SSA 2026 wage base](https://www.ssa.gov/oact/COLA/cbb.html)
- [IRS Social Security and Medicare rates](https://www.irs.gov/taxtopics/tc751)
- [IRS Additional Medicare rules](https://www.irs.gov/businesses/small-businesses-self-employed/questions-and-answers-for-the-additional-medicare-tax)
- [IRS senior deduction](https://www.irs.gov/newsroom/check-your-eligibility-for-the-new-enhanced-deduction-for-seniors)
- [Senior deduction statute](https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title26-section151&num=0&edition=prelim)
- [IRS retirement contribution tax treatment](https://www.irs.gov/retirement-plans/retirement-plan-faqs-regarding-contributions-are-retirement-plan-contributions-subject-to-withholding-for-fica-medicare-or-federal-income-tax)

- [IRS 2026 contribution limits](https://www.irs.gov/retirement-plans/cola-increases-for-dollar-limitations-on-benefits-and-contributions)
- [IRS community property treatment](https://www.irs.gov/publications/p555)
- [Oregon 2026 estimated tax rates](https://www.oregon.gov/dor/forms/FormsPubs/publication-or-estimate_101-026_2026.pdf)
- [Updated Oregon 2026 indexed amounts](https://www.oregon.gov/dor/forms/FormsPubs/withholding-tax-formulas_206-436_2026.pdf)
- [Oregon nonresident return proration](https://www.oregon.gov/dor/forms/FormsPubs/form-or-40-n_101-048_2025.pdf)
- [Oregon income-tax statutes](https://www.oregonlegislature.gov/bills_laws/ors/ors316.html)
- [Washington enacted income-tax bill report](https://lawfilesext.leg.wa.gov/biennium/2025-26/Pdf/Bill%20Reports/Senate/6346-S.E%20SBR%20FBR%2026.pdf)
