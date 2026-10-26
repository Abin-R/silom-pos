"""Till permissions ticked per staff member on Backoffice › Staff.

Unticking a box is a refusal from the API, not just a hidden button: an
older till build that still shows Adjust stock gets a 403 and stock stays
put. The role only supplies the starting ticks.

Run:
    python manage.py test bravepos.tests.test_staff_permissions \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

import importlib

from django.apps import apps as django_apps
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from bravepos import permissions
from bravepos.models import AuditLog, BranchSession, Customer, Staff, StockMovement
from bravepos.tests.factories import make_branch, make_product


def till_login(client, branch, staff, pin):
    r = client.post('/api/auth/pin-login', {
        'branch_id': str(branch.id), 'staff_id': str(staff.id), 'pin': pin,
    }, format='json')
    assert r.status_code == 200, r.content
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {r.json()['token']}")
    return r.json()


def make_staff(branch, role, name, pin, perms=None):
    member = Staff(name=name, email=f'{name.lower()}@tills.invalid', role=role,
                   active=True, permissions=perms)
    member.set_pin(pin)
    member.save()
    member.branches.add(branch)
    return member


class CatalogueTests(TestCase):

    def test_role_defaults_match_what_each_role_could_do_before(self):
        self.assertEqual(permissions.ROLE_DEFAULTS['admin'], permissions.KEYS)
        self.assertEqual(
            permissions.ROLE_DEFAULTS['cashier'],
            ['manage_inventory', 'adjust_stock', 'manage_customers'])

    def test_migration_copy_matches_the_catalogue(self):
        migration = importlib.import_module('bravepos.migrations.0073_staff_permissions')
        self.assertEqual(migration.ROLE_DEFAULTS, permissions.ROLE_DEFAULTS)

    def test_migration_backfill_ticks_role_defaults(self):
        branch = make_branch()
        admin = make_staff(branch, 'admin', 'Boss', '1234')
        cashier = make_staff(branch, 'cashier', 'Nok', '0000')
        viewer = make_staff(branch, 'viewer', 'Reports', '1111')
        migration = importlib.import_module('bravepos.migrations.0073_staff_permissions')
        migration.backfill(django_apps, None)
        for member in (admin, cashier, viewer):
            member.refresh_from_db()
        self.assertEqual(admin.permissions, permissions.ROLE_DEFAULTS['admin'])
        self.assertEqual(cashier.permissions, permissions.ROLE_DEFAULTS['cashier'])
        self.assertEqual(viewer.permissions, [])

    def test_never_customised_follows_the_role(self):
        self.assertTrue(Staff(role='cashier').can('adjust_stock'))
        self.assertFalse(Staff(role='cashier').can('manage_products'))

    def test_adjust_needs_inventory(self):
        self.assertEqual(permissions.clean(['adjust_stock']), [])


class TillApiTests(TestCase):

    def setUp(self):
        self.branch = make_branch()
        self.product = make_product(self.branch, stock=10)
        self.client = APIClient()

    def login(self, perms, role='cashier'):
        member = make_staff(self.branch, role, 'Nok', '0000', perms)
        till_login(self.client, self.branch, member, '0000')
        return member

    def test_login_returns_permissions(self):
        member = make_staff(self.branch, 'cashier', 'Nok', '0000', ['manage_inventory'])
        body = till_login(self.client, self.branch, member, '0000')
        self.assertEqual(body['staff']['permissions'], ['manage_inventory'])
        me = self.client.get('/api/auth/me').json()
        self.assertEqual(me['staff']['permissions'], ['manage_inventory'])

    def test_adjust_refused_without_permission(self):
        self.login(['manage_inventory'])
        r = self.client.post('/api/stock-movements', {
            'product_id': str(self.product.id), 'type': 'adjust', 'qty': 99,
        }, format='json')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['code'], 'perm_denied')
        r = self.client.post('/api/stock-documents', {
            'type': 'adjust',
            'items': [{'product_id': str(self.product.id), 'qty': 89,
                       'before_qty': 10, 'reconcile_qty': 99}],
        }, format='json')
        self.assertEqual(r.status_code, 403)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)

    def test_in_out_check_still_work_and_are_attributed(self):
        member = self.login(['manage_inventory'])
        for type_ in ('in', 'out', 'check'):
            r = self.client.post('/api/stock-movements', {
                'product_id': str(self.product.id), 'type': type_, 'qty': 2,
            }, format='json')
            self.assertEqual(r.status_code, 201, r.content)
            self.assertEqual(r.json()['created_by'], 'Nok')
        r = self.client.post('/api/stock-documents', {
            'type': 'in', 'items': [{'product_id': str(self.product.id), 'qty': 3}],
        }, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(StockMovement.objects.filter(staff=member).count(), 4)

    def test_adjust_allowed_with_permission(self):
        self.login(['manage_inventory', 'adjust_stock'])
        r = self.client.post('/api/stock-movements', {
            'product_id': str(self.product.id), 'type': 'adjust', 'qty': 7,
        }, format='json')
        self.assertEqual(r.status_code, 201)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 7)

    def test_no_inventory_means_no_stock_moves(self):
        self.login([])
        r = self.client.post('/api/stock-movements', {
            'product_id': str(self.product.id), 'type': 'in', 'qty': 1,
        }, format='json')
        self.assertEqual(r.status_code, 403)

    def test_products_need_manage_products(self):
        self.login(permissions.ROLE_DEFAULTS['cashier'])
        self.assertEqual(self.client.get('/api/products').status_code, 200)
        r = self.client.patch(f'/api/products/{self.product.id}', {'name': 'X'}, format='json')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['permission'], 'manage_products')

    def test_customers_registering_open_editing_gated(self):
        self.login([])
        r = self.client.post('/api/customers', {'name': 'Walk-in', 'phone': '0812345678'},
                             format='json')
        self.assertEqual(r.status_code, 201, r.content)
        cid = r.json()['id']
        r = self.client.patch(f'/api/customers/{cid}', {'name': 'Renamed'}, format='json')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(Customer.objects.get(id=cid).name, 'Walk-in')

    def test_branch_edit_needs_manage_settings(self):
        self.login(permissions.ROLE_DEFAULTS['cashier'])
        r = self.client.patch(f'/api/branches/{self.branch.id}', {'name': 'X'}, format='json')
        self.assertEqual(r.status_code, 403)


class BackofficeTests(TestCase):

    password = 'correct-horse-battery'

    def setUp(self):
        self.branch = make_branch('Silom')
        self.other = make_branch('Asok')
        self.member = make_staff(self.branch, 'cashier', 'Nok', '0000',
                                 list(permissions.ROLE_DEFAULTS['cashier']))
        self.far = make_staff(self.other, 'cashier', 'Ploy', '0000',
                              list(permissions.ROLE_DEFAULTS['cashier']))

    def account(self, role):
        user = Staff(name='Boss', username='boss', email='boss@x.invalid', role=role,
                     active=True, backoffice_access=True)
        user.set_password(self.password)
        user.save()
        self.assertTrue(self.client.login(username='boss', password=self.password))

    def save(self, perms, **extra):
        data = {'name': 'Nok', 'role': 'cashier', 'active': 'on', 'pin': '',
                'perms_form': '1', 'perm': perms}
        data.update(extra)
        return self.client.post(reverse('backoffice:staff_detail', args=[self.member.id]), data)

    def test_page_shows_ticked_boxes(self):
        self.account('admin')
        html = self.client.get(
            reverse('backoffice:staff_detail', args=[self.member.id])).content.decode()
        self.assertIn('Till permissions', html)
        self.assertIn('value="adjust_stock" data-parent="manage_inventory"\n               checked', html)

    def test_admin_untick_saves_signs_out_and_audits(self):
        self.account('admin')
        BranchSession.objects.create(branch=self.branch, staff=self.member,
                                     token=BranchSession.new_token())
        self.save(['manage_inventory', 'manage_customers'])
        self.member.refresh_from_db()
        self.assertEqual(self.member.permissions, ['manage_inventory', 'manage_customers'])
        self.assertFalse(BranchSession.objects.filter(staff=self.member).exists())
        self.assertTrue(AuditLog.objects.filter(object_id=str(self.member.id)).exists())

    def test_non_admin_cannot_change_boxes(self):
        self.account('cashier')
        self.save([])
        self.member.refresh_from_db()
        self.assertEqual(self.member.permissions, permissions.ROLE_DEFAULTS['cashier'])

    def test_apply_to_other_branches(self):
        self.account('admin')
        self.save(['manage_inventory', 'manage_customers'], apply_branch=[str(self.other.id)])
        self.far.refresh_from_db()
        self.assertEqual(self.far.permissions, ['manage_inventory', 'manage_customers'])

    def test_staff_list_flags_restricted(self):
        self.account('admin')
        self.member.permissions = ['manage_inventory', 'manage_customers']
        self.member.save()
        html = self.client.get(reverse('backoffice:staff_list'),
                               {'branch': str(self.branch.id)}).content.decode()
        self.assertIn('No adjust stock', html)
