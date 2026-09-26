"""Invented tax examples; independent arithmetic checks, no household data."""
import unittest

from planning_taxes import federal_return


def example_person(**changes):
    return dict(age=40, wages=60000, pretax_savings=0, roth_savings=0,
                taxable_withdrawals=0) | changes


class FederalPlanningTaxTests(unittest.TestCase):
    def test_single_standard_deduction_and_brackets(self):
        result = federal_return([example_person()], 'single')
        # 60,000 - 16,100 = 43,900; 12,400 at 10%, then 31,500 at 12%.
        self.assertEqual(result['federal_income_tax'], 5020)
        self.assertEqual(result['federal_payroll_tax'], 4590)
        self.assertEqual(result['federal_total'], 9610)

    def test_joint_return_aggregates_income_once(self):
        result = federal_return([example_person(), example_person()], 'joint')
        self.assertEqual(result['federal_income_tax'], 10040)
        self.assertEqual(result['federal_payroll_tax'], 9180)

    def test_social_security_cap_is_per_worker(self):
        two_workers = federal_return([example_person(wages=200000), example_person(wages=200000)], 'joint')
        one_worker = federal_return([example_person(wages=400000), example_person(wages=0)], 'joint')
        # Two caps of 184,500 at 6.2%, plus Medicare and 0.9% above 250,000.
        self.assertEqual(two_workers['federal_payroll_tax'], 30028)
        self.assertEqual(one_worker['federal_payroll_tax'], 18589)

    def test_pretax_reduces_income_tax_but_not_payroll_tax(self):
        pretax = federal_return([example_person(pretax_savings=10000)], 'single')
        roth = federal_return([example_person(roth_savings=10000)], 'single')
        self.assertEqual(pretax['federal_income_tax'], 3820)
        self.assertEqual(roth['federal_income_tax'], 5020)
        self.assertEqual(pretax['federal_payroll_tax'], roth['federal_payroll_tax'])

    def test_retirement_distributions_are_not_wages(self):
        result = federal_return([example_person(wages=0, taxable_withdrawals=60000)], 'single')
        self.assertEqual(result['federal_income_tax'], 5020)
        self.assertEqual(result['federal_payroll_tax'], 0)

    def test_senior_deduction_expires_after_2028(self):
        person = example_person(age=65, wages=0, taxable_withdrawals=60000)
        current = federal_return([person], 'single', year=2028)
        future = federal_return([person], 'single', year=2029)
        self.assertEqual(current['federal_income_tax'], 4054)
        self.assertEqual(future['federal_income_tax'], 4774)

    def test_senior_phaseout_and_separate_filing(self):
        person = example_person(age=65, wages=0, taxable_withdrawals=100000)
        result = federal_return([person], 'single')
        # Senior deduction is 6,000 - 6% of (100,000 - 75,000) = 4,500.
        self.assertEqual(result['federal_income_tax'], 11729)
        separate = federal_return([person], 'separate')
        self.assertEqual(separate['federal_income_tax'], 12807)

    def test_indexed_thresholds_but_fixed_additional_medicare_threshold(self):
        result = federal_return([example_person(wages=300000)], 'single', factor=2, year=2030)
        self.assertEqual(result['federal_payroll_tax'], 23850)

    def test_rejects_incomplete_invalid_and_misgrouped_people(self):
        for people, status in (([], 'single'), ([example_person()], 'joint'),
                               ([example_person(), example_person()], 'single'),
                               ([example_person()], 'unknown'), ([{}], 'single'),
                               ([example_person(wages=float('nan'))], 'single'),
                               ([example_person(age=True)], 'single'),
                               ([example_person(age=40.5)], 'single'),
                               ([example_person(pretax_savings=60001)], 'single')):
            with self.subTest(status=status), self.assertRaises(ValueError):
                federal_return(people, status)
