# Household tax estimates: foundation in progress

`planning_taxes.py` provides a tested federal calculation for one tax return.
It is not yet connected to Plan. The existing preview still excludes taxes.
State tax policies and household profile screens remain to be implemented.

Each person supplies gross W-2 wages, eligible pre-tax contributions, Roth
contributions, age, and taxable retirement distributions. Joint returns combine
two people for income tax while keeping each worker's Social Security wage cap.
Other filing statuses calculate one return per person. This does not establish
eligibility to use a particular filing status. Roth saving does not reduce
taxable wages; pre-tax retirement saving does not reduce FICA wages.

The federal model uses 2026 ordinary-income brackets, the standard deduction,
age-related deductions, Social Security, Medicare, and Additional Medicare.
The temporary senior deduction phases out with income and ends after 2028.
For later years, callers supply a planning inflation factor for indexed amounts.
Additional Medicare thresholds and the temporary senior phaseout stay fixed.
Indexing is a scenario assumption, not a prediction of future tax law.

Contributions supplied to this module must already meet the applicable plan
limits. It does not establish deductibility or implement retirement plan limits.
It excludes itemization, dependents and tax credits, capital gains and dividends,
AMT, self-employment, benefits, early-withdrawal penalties, and local/state taxes.
It must not be presented as a complete household tax estimate on its own.

The household integration needs residence and physical work states separately,
state sourcing/reciprocity and credits, pre-tax versus Roth investment balances,
and an adjustment for income tax payments already counted in spending history.
Unsupported state combinations must be identified, never assumed tax-free.
Profile data must use the encrypted vault; development remains read-only.

## Official sources checked September 25, 2026

- [IRS 2026 brackets and deductions](https://www.irs.gov/irb/2025-45_IRB)
- [SSA 2026 wage base](https://www.ssa.gov/oact/COLA/cbb.html)
- [IRS Social Security and Medicare rates](https://www.irs.gov/taxtopics/tc751)
- [IRS Additional Medicare rules](https://www.irs.gov/businesses/small-businesses-self-employed/questions-and-answers-for-the-additional-medicare-tax)
- [IRS senior deduction](https://www.irs.gov/newsroom/check-your-eligibility-for-the-new-enhanced-deduction-for-seniors)
- [Senior deduction statute](https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title26-section151&num=0&edition=prelim)
- [IRS retirement contribution tax treatment](https://www.irs.gov/retirement-plans/retirement-plan-faqs-regarding-contributions-are-retirement-plan-contributions-subject-to-withholding-for-fica-medicare-or-federal-income-tax)
