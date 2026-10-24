# Copyright 2015-2021 Akretion France (http://www.akretion.com/)
# @author: Alexis de Lattre <alexis.delattre@akretion.com>
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).


from types import SimpleNamespace
from unittest.mock import patch

from facturx import get_facturx_xml_from_pdf

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase
from odoo.tools import file_open, mute_logger


class TestFacturx(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.ref("base.main_company")
        adjustment_credit_account_id = (
            cls.env["account.account"]
            .create(
                {
                    "company_ids": [Command.set([cls.company.id])],
                    "code": "758FXADJUST",
                    "name": "Adjustment income account",
                    "account_type": "income",
                }
            )
            .id
        )
        adjustment_debit_account_id = (
            cls.env["account.account"]
            .create(
                {
                    "company_ids": [Command.set([cls.company.id])],
                    "code": "658FXADJUST",
                    "name": "Adjustment expense account",
                    "account_type": "expense",
                }
            )
            .id
        )
        cls.company.write(
            {
                "adjustment_credit_account_id": adjustment_credit_account_id,
                "adjustment_debit_account_id": adjustment_debit_account_id,
            }
        )
        cls.expense_account = cls.env["account.account"].search(
            [
                ("company_ids", "=", cls.company.id),
                ("account_type", "=", "expense"),
            ],
            limit=1,
        )
        cls.purchase_tax = cls.env["account.tax"].search(
            [
                ("company_id", "=", cls.company.id),
                ("type_tax_use", "=", "purchase"),
                ("amount_type", "=", "percent"),
                ("amount", ">", 5),
            ],
            limit=1,
        )
        cls.jolie_boutique_partner = cls.env["res.partner"].create(
            {
                "name": "Ma jolie boutique",
                "is_company": True,
                "country_id": cls.env.ref("base.fr").id,
                "vat": "FR11999999998",
                "invoice_import_shrink_lines": "single",
                "invoice_import_account_id": cls.expense_account.id,
                "invoice_import_tax_ids": [Command.set(cls.purchase_tax.ids)],
            }
        )

    # avoid warning logs pypdf._reader: incorrect startxref pointer(1)
    @mute_logger("pypdf._reader")
    def test_import_facturx_invoice(self):
        sample_files = {
            "Facture_FR_BASICWL.pdf": {
                "invoice_number": "FA-2017-0010",
                "amount_untaxed": 624.90,
                "amount_total": 671.15,
                "invoice_date": "2017-11-13",
                "partner_id": self.jolie_boutique_partner.id,
            },
            "Facture_FR_EN16931.pdf": {
                "invoice_number": "FA-2017-0010",
                "amount_untaxed": 624.90,
                "amount_total": 671.15,
                "invoice_date": "2017-11-13",
                "partner_id": self.jolie_boutique_partner.id,
            },
            "Facture_FR_EXTENDED.pdf": {
                "invoice_number": "FA-2017-0010",
                "amount_untaxed": 624.90,
                "amount_total": 671.15,
                "invoice_date": "2017-11-13",
                "partner_id": self.jolie_boutique_partner.id,
            },
        }
        amo = self.env["account.move"]
        cur = self.env.ref("base.EUR")
        for inv_file, res_dict in sample_files.items():
            f = file_open(
                "account_invoice_import_facturx/tests/files/" + inv_file, "rb"
            )
            pdf_file = f.read()
            f.close()
            wiz = self.env["account.invoice.import"].create(
                {
                    "invoice_attachment_ids": [
                        Command.create({"raw": pdf_file, "name": inv_file})
                    ],
                    "company_id": self.company.id,
                    "debug_disable_company_check": True,
                }
            )
            wiz.import_invoices_button()
            invoices = amo.search(
                [
                    ("state", "=", "draft"),
                    ("move_type", "in", ("in_invoice", "in_refund")),
                    ("ref", "=", res_dict["invoice_number"]),
                    ("company_id", "=", self.company.id),
                ]
            )
            self.assertEqual(len(invoices), 1)
            inv = invoices[0]
            self.assertEqual(inv.move_type, res_dict.get("type", "in_invoice"))
            self.assertEqual(
                fields.Date.to_string(inv.invoice_date), res_dict["invoice_date"]
            )
            if res_dict.get("invoice_date_due"):
                self.assertEqual(
                    fields.Date.to_string(inv.invoice_date_due),
                    res_dict["invoice_date_due"],
                )
            self.assertEqual(
                inv.partner_id.id,
                res_dict["partner_id"],
            )
            self.assertFalse(
                cur.compare_amounts(
                    inv.amount_untaxed,
                    res_dict["amount_untaxed"],
                )
            )
            self.assertFalse(
                cur.compare_amounts(
                    inv.amount_total,
                    res_dict["amount_total"],
                )
            )
            # Delete because several sample invoices have the same number
            invoices.unlink()

    def _batch_file(self, filename, name=None):
        with file_open("account_invoice_import/tests/pdf/" + filename, "rb") as f:
            return {"raw": f.read(), "name": name or filename}

    def _facturx_xml(self, name):
        pdf = self._batch_file("Facture_FR_EN16931.pdf")["raw"]
        xml_filename, xml_bytes = get_facturx_xml_from_pdf(pdf)
        return {"raw": xml_bytes, "name": name}

    def _import_batch(self, files):
        wiz = self.env["account.invoice.import"].create(
            {
                "invoice_attachment_ids": [Command.create(vals) for vals in files],
                "company_id": self.company.id,
                "debug_disable_company_check": True,
            }
        )
        bill_domain = [
            ("move_type", "in", ("in_invoice", "in_refund")),
            ("company_id", "=", self.company.id),
        ]
        before = self.env["account.move"].search(bill_domain)
        action = wiz.import_invoices_button()
        # every vendor bill created by the batch, including empty drafts
        invoices = self.env["account.move"].search(bill_domain) - before
        return action["params"]["message"], invoices

    def _attachment_names(self, invoice):
        return set(
            self.env["ir.attachment"]
            .search([("res_model", "=", "account.move"), ("res_id", "=", invoice.id)])
            .mapped("name")
        )

    @mute_logger("pypdf._reader")
    def test_import_batch_pdf_paired_by_name(self):
        # The PDF can't be read: only the file name pairs it with the XML
        message, invoices = self._import_batch(
            [
                self._facturx_xml("supplier-invoice.xml"),
                self._batch_file("unknown_invoice.pdf", "supplier-invoice.pdf"),
            ]
        )
        self.assertEqual(len(invoices), 1, message)
        self.assertEqual(invoices.partner_id, self.jolie_boutique_partner)
        self.assertLessEqual(
            {"supplier-invoice.xml", "supplier-invoice.pdf"},
            self._attachment_names(invoices),
        )

    @mute_logger("pypdf._reader")
    def test_import_batch_pdf_paired_with_best_xml(self):
        # Two sellers use the same invoice number: the PDF goes to the XML
        # with the same file name, whatever the order of the files
        other_seller_xml = self._facturx_xml("other.xml")
        other_seller_xml["raw"] = other_seller_xml["raw"].replace(
            b"FR11999999998", b"FR44732829320"
        )
        message, invoices = self._import_batch(
            [
                other_seller_xml,
                self._facturx_xml("FA-2017-0010.xml"),
                self._batch_file("unknown_invoice.pdf", "FA-2017-0010.pdf"),
            ]
        )
        self.assertEqual(len(invoices), 2, message)
        bill = invoices.filtered(lambda inv: inv.partner_id)
        self.assertEqual(bill.partner_id, self.jolie_boutique_partner)
        self.assertIn("FA-2017-0010.pdf", self._attachment_names(bill))
        self.assertNotIn("FA-2017-0010.pdf", self._attachment_names(invoices - bill))

    @mute_logger("pypdf._reader")
    def test_import_batch_pdf_not_paired_when_ambiguous(self):
        # The invoice number of two XML files is in the PDF file name: the PDF
        # is not attached to one of them at random
        other_seller_xml = self._facturx_xml("other.xml")
        other_seller_xml["raw"] = other_seller_xml["raw"].replace(
            b"FR11999999998", b"FR44732829320"
        )
        message, invoices = self._import_batch(
            [
                other_seller_xml,
                self._facturx_xml("export.xml"),
                self._batch_file("unknown_invoice.pdf", "Invoice FA-2017-0010.pdf"),
            ]
        )
        # 2 bills from the XML files and an empty draft for the PDF
        self.assertEqual(len(invoices), 3, message)
        for invoice in invoices.filtered(lambda inv: inv.ref == "FA-2017-0010"):
            self.assertNotIn(
                "Invoice FA-2017-0010.pdf", self._attachment_names(invoice)
            )

    def test_import_batch_add_paired_file(self):
        wizard = self.env["account.invoice.import"]
        import_config = {"company": self.company}
        wizard._update_import_config(import_config)
        parsed_inv = {"BG-24": [{"BT-125": b"embedded", "BT-125-2": "invoice.pdf"}]}
        # same content as the PDF embedded in the XML: not attached twice
        wizard._add_paired_file(
            parsed_inv, SimpleNamespace(name="copy.pdf", raw=b"embedded"), import_config
        )
        self.assertEqual(len(parsed_inv["BG-24"]), 1)
        # same name but another content: kept
        wizard._add_paired_file(
            parsed_inv, SimpleNamespace(name="invoice.pdf", raw=b"other"), import_config
        )
        self.assertEqual(len(parsed_inv["BG-24"]), 2)

    @mute_logger("pypdf._reader")
    def test_import_batch_rollback_failed_file(self):
        # A file that fails after its vendor bill was created leaves nothing
        # behind, and the next files are imported
        wizard_class = type(self.env["account.invoice.import"])
        create_invoice = wizard_class.create_invoice

        def create_then_fail(wizard, parsed_inv, import_config):
            invoice = create_invoice(wizard, parsed_inv, import_config)
            if import_config["filename"] == "first.pdf":
                raise UserError(wizard.env._("simulated failure"))
            return invoice

        with patch.object(wizard_class, "create_invoice", create_then_fail):
            message, invoices = self._import_batch(
                [
                    self._batch_file("Facture_FR_EN16931.pdf", "first.pdf"),
                    self._batch_file("Facture_FR_EN16931.pdf", "second.pdf"),
                ]
            )
        self.assertIn("first.pdf: simulated failure", message)
        # had the first bill not been rolled back, the second file would be
        # refused as a duplicate
        self.assertEqual(len(invoices), 1, message)
        self.assertIn("second.pdf", self._attachment_names(invoices))
        self.assertNotIn("first.pdf", self._attachment_names(invoices))

    @mute_logger("pypdf._reader")
    def test_import_batch_pdf_paired_by_invoice_number(self):
        # The PDF can't be read, but its file name carries the invoice number
        message, invoices = self._import_batch(
            [
                self._facturx_xml("factur-x.xml"),
                self._batch_file("unknown_invoice.pdf", "Invoice FA-2017-0010.pdf"),
            ]
        )
        self.assertEqual(len(invoices), 1, message)
        self.assertIn("Invoice FA-2017-0010.pdf", self._attachment_names(invoices))
        self.assertNotIn("Invoice FA-2017-0010.pdf:", message)

    @mute_logger("pypdf._reader")
    def test_import_batch_pdf_paired_by_content(self):
        # Same seller, number and total amount once both files are read
        message, invoices = self._import_batch(
            [
                self._facturx_xml("export.xml"),
                self._batch_file("Facture_FR_EN16931.pdf", "scan.pdf"),
            ]
        )
        self.assertEqual(len(invoices), 1, message)
        self.assertIn("scan.pdf", self._attachment_names(invoices))

    @mute_logger(
        "pypdf._reader",
        "odoo.addons.account_invoice_import.wizard.account_invoice_import",
    )
    def test_import_batch_error_on_one_file(self):
        message, invoices = self._import_batch(
            [
                {"raw": b"not a PDF file", "name": "broken.pdf"},
                self._batch_file("Facture_FR_EN16931.pdf"),
                self._batch_file("unknown_invoice.pdf"),
            ]
        )
        # broken.pdf is reported; the unknown PDF gives an empty draft bill
        # (behavior of the module for a PDF it can't read)
        self.assertIn("broken.pdf: ", message)
        self.assertEqual(len(invoices), 2, message)
        self.assertEqual(
            len(invoices.filtered(lambda inv: inv.ref == "FA-2017-0010")), 1
        )

    @mute_logger(
        "pypdf._reader",
        "odoo.addons.account_invoice_import.wizard.account_invoice_import",
    )
    def test_import_single_file_error_raises(self):
        wiz = self.env["account.invoice.import"].create(
            {
                "invoice_attachment_ids": [
                    Command.create({"raw": b"not a PDF file", "name": "broken.pdf"})
                ],
                "company_id": self.company.id,
            }
        )
        with self.assertRaisesRegex(UserError, "broken.pdf: unreadable or damaged"):
            wiz.import_invoices_button()
