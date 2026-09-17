import base64
import io
import sqlite3
import unittest
from unittest.mock import patch

from PIL import Image

from schema import create_schema
import statement_import as statements


def text_pdf():
    stream = b'BT /F1 12 Tf 30 180 Td (Example statement transaction September 2026 Cafe 12.34 USD) Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Count 1 /Kids [3 0 R] >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 220] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream']
    data = b'%PDF-1.4\n'
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += str(index).encode() + b' 0 obj\n' + obj + b'\nendobj\n'
    start = len(data)
    data += b'xref\n0 6\n0000000000 65535 f \n'
    data += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    data += b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n' + str(start).encode() + b'\n%%EOF'
    return data


class StatementImportTests(unittest.TestCase):
    def test_account_sections_match_only_unique_endings_and_leave_unknowns_unassigned(self):
        accounts = [{'id': 'checking', 'type': 'depository', 'mask': '1111'},
                    {'id': 'savings', 'type': 'depository', 'mask': '2222'},
                    {'id': 'another-savings', 'type': 'depository', 'mask': '2222'}]
        rows = [{'account_label': 'Checking', 'account_last4': '1111', 'account_type': 'depository'},
                {'account_label': 'Checking', 'account_last4': '1111', 'account_type': 'depository'},
                {'account_label': 'Savings', 'account_last4': '2222', 'account_type': 'depository'},
                {'account_label': '', 'account_last4': '', 'account_type': ''}]
        groups = statements.group_accounts(rows, accounts, 'checking')
        self.assertEqual(len(groups), 3)
        self.assertEqual([group['account_id'] for group in groups], ['checking', '', ''])
        self.assertEqual([row['account_group'] for row in rows], ['0', '0', '1', '2'])

    def test_extraction_does_not_keep_full_account_identifiers(self):
        row = {'date': '2026-01-02', 'description': 'Example', 'amount': '1.00', 'direction': 'money_out',
               'evidence': 'Example', 'account_label': 'Checking 123456789', 'account_last4': '123456789', 'account_type': 'other'}
        with patch.object(statements, 'local_model', return_value={'transactions': [row]}):
            result = statements.extract_page({'number': 1, 'text': 'A' * 100, 'image': ''}, 'unknown')
        self.assertEqual(result['transactions'][0]['account_last4'], '')
        self.assertNotIn('123456789', result['transactions'][0]['account_label'])

    def test_full_pdf_accepts_25_pages_and_rejects_26_without_truncation(self):
        with Image.new('RGB', (100, 80), 'white') as image:
            for count in (9, 25, 26):
                buffer = io.BytesIO()
                image.save(buffer, format='PDF', save_all=True, append_images=[image] * (count - 1))
                if count == 26:
                    with self.assertRaisesRegex(statements.StatementError, '25 pages'):
                        statements.document_pages(buffer.getvalue())
                else:
                    pages = statements.document_pages(buffer.getvalue())
                    self.assertEqual([page['number'] for page in pages], list(range(1, count + 1)))

    def test_date_context_uses_cover_and_later_headers_without_reimporting_them(self):
        pages = [{'number': 1, 'text': '', 'image': 'cover'},
                 {'number': 2, 'text': 'Statement period December 15, 2025 through January 14, 2026. ' * 3, 'image': 'transactions'}]
        context = statements.date_context(pages)
        context.update(statement_start='2025-12-15', statement_end='2026-01-14')
        with patch.object(statements, 'local_model', return_value={'transactions': []}) as model:
            statements.extract_page(pages[1], 'depository', context)
        payload = model.call_args.args[0]
        self.assertEqual(payload['messages'][1]['images'], ['cover'])
        self.assertIn('2025-12-15', payload['messages'][1]['content'])
        self.assertIn('Do not extract its transactions again', payload['messages'][1]['content'])
        self.assertIn('Never assume the current year', payload['messages'][0]['content'])

    def test_transaction_range_is_derived_from_valid_dates_without_period_restrictions(self):
        rows = [{'date': value, 'description': 'Example', 'amount': '1.00', 'direction': 'money_out'}
                for value in ('2025-12-30', '2026-01-02', '', '2026-02-30')]
        self.assertEqual(statements.date_range(rows), ('2025-12-30', '2026-01-02'))
        self.assertEqual(statements.validate_row(rows[0])['date'], '2025-12-30')
        self.assertEqual(statements.date_range(rows[2:]), ('', ''))

    def test_pdf_text_and_image_preview_are_extracted_in_memory(self):
        pages = statements.document_pages(text_pdf())
        self.assertEqual(len(pages), 1)
        self.assertIn('Cafe', pages[0]['text'])
        self.assertTrue(base64.b64decode(pages[0]['image']).startswith(b'\xff\xd8'))

    def test_image_input_and_scanned_pdf_produce_vision_pages(self):
        image = Image.new('RGB', (100, 80), 'white')
        for format_name in ['PNG', 'JPEG', 'PDF']:
            buffer = io.BytesIO()
            image.save(buffer, format=format_name)
            pages = statements.document_pages(buffer.getvalue())
            self.assertEqual(len(pages), 1)
            self.assertEqual(pages[0]['text'].strip(), '')
            self.assertTrue(pages[0]['image'])
        with self.assertRaises(statements.StatementError):
            statements.document_pages(b'<script>not a statement</script>')
        with patch.object(statements, 'MAX_UPLOAD_BYTES', 10):
            with self.assertRaises(statements.StatementError):
                statements.document_pages(b'x' * 11)

    def test_extraction_uses_images_only_when_text_is_unavailable(self):
        response = {'transactions': [], 'account_last4': '', 'warnings': []}
        for text, expected_image in [('', True), ('A' * 100, False)]:
            with patch.object(statements, 'local_model', return_value=response) as model:
                statements.extract_page({'number': 1, 'text': text, 'image': 'example'}, 'credit')
            payload = model.call_args.args[0]
            self.assertEqual('images' in payload['messages'][1], expected_image)
            self.assertIn('Never invent', payload['messages'][0]['content'])
            self.assertIn('untrusted data', payload['messages'][0]['content'])
            self.assertEqual(payload['model'], statements.MODEL)

    def test_row_validation_rejects_guessed_or_invalid_values(self):
        row = {'date': '2026-09-05', 'description': 'Example Cafe', 'amount': '12.34', 'direction': 'money_out'}
        self.assertEqual(statements.validate_row(row)['amount'], 1234)
        self.assertEqual(statements.validate_row({**row, 'direction': 'money_in'})['amount'], -1234)
        for change in [{'date': ''}, {'date': '09/05'}, {'date': '20260905'}, {'date': '2026-09-31'}, {'amount': 'NaN'}, {'amount': '1e4'}, {'amount': '-12.34'}, {'amount': '12.345'}, {'amount': '0.00'}, {'direction': ''}, {'description': ''}]:
            with self.subTest(change=change), self.assertRaises(statements.StatementError):
                statements.validate_row({**row, **change})

    def test_duplicate_checks_are_account_scoped_and_keep_repeated_rows_visible(self):
        connection = sqlite3.connect(':memory:')
        try:
            create_schema(connection)
            connection.execute("INSERT INTO transactions (id, account_id, amount, currency, description, pending, transacted_at, category) VALUES ('old', 'account', 1234, 'USD', 'Example Cafe', 0, '2026-09-05', 'Dining')")
            row = {'date': '2026-09-05', 'amount': 1234, 'description': 'Example Cafe'}
            duplicates = statements.find_duplicates(connection, 'account', [row, row])
            self.assertEqual(len(duplicates), 2)
            self.assertTrue(all(duplicates))
            self.assertEqual(statements.find_duplicates(connection, 'other', [row]), [''])
            self.assertIn('within three days', statements.find_duplicates(connection, 'account', [{**row, 'date': '2026-09-07'}])[0])
            self.assertTrue(statements.find_duplicates(connection, 'other', [row, row])[1])
        finally:
            connection.close()

    def test_local_model_rejects_redirects(self):
        with self.assertRaises(statements.StatementError):
            statements.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.com')


if __name__ == '__main__':
    unittest.main()
