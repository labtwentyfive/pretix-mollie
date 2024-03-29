import hashlib
import json
import logging
import urllib.parse
from collections import OrderedDict
from datetime import timedelta

import requests
from django import forms
from django.core import signing
from django.http import HttpRequest
from django.template.loader import get_template
from django.urls import reverse
from django.utils.crypto import get_random_string
from django.utils.http import urlquote
from django.utils.translation import pgettext, ugettext_lazy as _
from pretix.base.models import Event, OrderPayment, OrderRefund
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.base.settings import SettingsSandbox
from pretix.helpers.urls import build_absolute_uri as build_global_uri
from pretix.multidomain.urlreverse import build_absolute_uri
from requests import HTTPError

from .forms import MollieKeyValidator

logger = logging.getLogger(__name__)


class MollieSettingsHolder(BasePaymentProvider):
    identifier = 'mollie'
    verbose_name = _('Mollie')
    is_enabled = False
    is_meta = True

    def __init__(self, event: Event):
        super().__init__(event)
        self.settings = SettingsSandbox('payment', 'mollie', event)

    def get_connect_url(self, request):
        request.session['payment_mollie_oauth_event'] = request.event.pk
        if 'payment_mollie_oauth_token' not in request.session:
            request.session['payment_mollie_oauth_token'] = get_random_string(32)
        return (
            "https://www.mollie.com/oauth2/authorize?client_id={}&redirect_uri={}"
            "&state={}&scope=payments.read+payments.write+refunds.read+refunds.write+profiles.read+organizations.read"
            "&response_type=code&approval_prompt=auto"
        ).format(
            self.settings.connect_client_id,
            urlquote(build_global_uri('plugins:pretix_mollie:oauth.return')),
            request.session['payment_mollie_oauth_token'],
        )

    def settings_content_render(self, request):
        if self.settings.connect_client_id and not self.settings.api_key:
            # Use Mollie Connect
            if not self.settings.access_token:
                return (
                    "<p>{}</p>"
                    "<a href='{}' class='btn btn-primary btn-lg'>{}</a>"
                ).format(
                    _('To accept payments via Mollie, you will need an account at Mollie. By clicking on the '
                      'following button, you can either create a new Mollie account connect pretix to an existing '
                      'one.'),
                    self.get_connect_url(request),
                    _('Connect with Mollie')
                )
            else:
                return (
                    "<button formaction='{}' class='btn btn-danger'>{}</button>"
                ).format(
                    reverse('plugins:pretix_mollie:oauth.disconnect', kwargs={
                        'organizer': self.event.organizer.slug,
                        'event': self.event.slug,
                    }),
                    _('Disconnect from Mollie')
                )

    @property
    def test_mode_message(self):
        if self.settings.connect_client_id and not self.settings.api_key:
            is_testmode = True
        else:
            is_testmode = 'test_' in self.settings.secret_key
        if is_testmode:
            return _('The Mollie plugin is operating in test mode. No money will actually be transferred.')
        return None

    @property
    def settings_form_fields(self):
        if self.settings.connect_client_id and not self.settings.api_key:
            # Mollie Connect
            if self.settings.access_token:
                fields = [
                    ('connect_org_name',
                     forms.CharField(
                         label=_('Mollie account'),
                         disabled=True
                     )),
                    ('connect_profile',
                     forms.ChoiceField(
                         label=_('Website profile'),
                         choices=self.settings.get('connect_profiles', as_type=list) or []
                     )),
                    ('endpoint',
                     forms.ChoiceField(
                         label=_('Endpoint'),
                         initial='live',
                         choices=(
                             ('live', pgettext('mollie', 'Live')),
                             ('test', pgettext('mollie', 'Testing')),
                         ),
                     )),
                ]
            else:
                return {}
        else:
            fields = [
                ('api_key',
                 forms.CharField(
                     label=_('Secret key'),
                     validators=(
                         MollieKeyValidator(['live_', 'test_']),
                     ),
                 )),
            ]
        d = OrderedDict(
            fields + [
                ('method_creditcard',
                 forms.BooleanField(
                     label=_('Credit card'),
                     required=False,
                 )),
                ('method_bancontact',
                 forms.BooleanField(
                     label=_('Bancontact'),
                     required=False,
                 )),
                ('method_banktransfer',
                 forms.BooleanField(
                     label=_('Bank transfer'),
                     required=False,
                 )),
                ('method_belfius',
                 forms.BooleanField(
                     label=_('Belfius Pay Button'),
                     required=False,
                 )),
                ('method_bitcoin',
                 forms.BooleanField(
                     label=_('Bitcoin'),
                     required=False,
                 )),
                ('method_eps',
                 forms.BooleanField(
                     label=_('EPS'),
                     required=False,
                 )),
                ('method_giropay',
                 forms.BooleanField(
                     label=_('giropay'),
                     required=False,
                 )),
                ('method_ideal',
                 forms.BooleanField(
                     label=_('iDEAL'),
                     required=False,
                 )),
                ('method_inghomepay',
                 forms.BooleanField(
                     label=_('ING Home’Pay'),
                     required=False,
                 )),
                ('method_kbc',
                 forms.BooleanField(
                     label=_('KBC/CBC Payment Button'),
                     required=False,
                 )),
                ('method_paysafecard',
                 forms.BooleanField(
                     label=_('paysafecard'),
                     required=False,
                 )),
                ('method_sofort',
                 forms.BooleanField(
                     label=_('Sofort'),
                     required=False,
                 )),
            ] + list(super().settings_form_fields.items())
        )
        d.move_to_end('_enabled', last=False)
        return d


class MollieMethod(BasePaymentProvider):
    method = ''
    abort_pending_allowed = False
    refunds_allowed = True

    def __init__(self, event: Event):
        super().__init__(event)
        self.settings = SettingsSandbox('payment', 'mollie', event)

    @property
    def settings_form_fields(self):
        return {}

    @property
    def identifier(self):
        return 'mollie_{}'.format(self.method)

    @property
    def is_enabled(self) -> bool:
        return self.settings.get('_enabled', as_type=bool) and self.settings.get('method_{}'.format(self.method),
                                                                                 as_type=bool)

    def payment_refund_supported(self, payment: OrderPayment) -> bool:
        return self.refunds_allowed

    def payment_partial_refund_supported(self, payment: OrderPayment) -> bool:
        return self.refunds_allowed

    def payment_prepare(self, request, payment):
        return self.checkout_prepare(request, None)

    def payment_is_valid_session(self, request: HttpRequest):
        return True

    @property
    def request_headers(self):
        headers = {}
        if self.settings.connect_client_id and self.settings.access_token:
            headers['Authorization'] = 'Bearer %s' % self.settings.access_token
        else:
            headers['Authorization'] = 'Bearer %s' % self.settings.api_key
        return headers

    def payment_form_render(self, request) -> str:
        template = get_template('pretix_mollie/checkout_payment_form.html')
        ctx = {'request': request, 'event': self.event, 'settings': self.settings}
        return template.render(ctx)

    def checkout_confirm_render(self, request) -> str:
        template = get_template('pretix_mollie/checkout_payment_confirm.html')
        ctx = {'request': request, 'event': self.event, 'settings': self.settings, 'provider': self}
        return template.render(ctx)

    def payment_can_retry(self, payment):
        return self._is_still_available(order=payment.order)

    def payment_pending_render(self, request, payment) -> str:
        if payment.info:
            payment_info = json.loads(payment.info)
        else:
            payment_info = None
        template = get_template('pretix_mollie/pending.html')
        ctx = {
            'request': request,
            'event': self.event,
            'settings': self.settings,
            'provider': self,
            'order': payment.order,
            'payment': payment,
            'payment_info': payment_info,
        }
        return template.render(ctx)

    def payment_control_render(self, request, payment) -> str:
        if payment.info:
            payment_info = json.loads(payment.info)
        else:
            payment_info = None
        template = get_template('pretix_mollie/control.html')
        ctx = {
            'request': request,
            'event': self.event,
            'settings': self.settings,
            'payment_info': payment_info,
            'payment': payment,
            'method': self.method,
            'provider': self,
        }
        return template.render(ctx)

    def execute_refund(self, refund: OrderRefund):
        payment = refund.payment.info_data.get('id')
        body = {
            'amount': {
                'currency': self.event.currency,
                'value': str(refund.amount)
            },
        }
        if self.settings.connect_client_id and self.settings.access_token:
            body['testmode'] = refund.payment.info_data.get('mode', 'live') == 'test'
        try:
            print(self.request_headers, body)
            req = requests.post(
                'https://api.mollie.com/v2/payments/{}/refunds'.format(payment),
                json=body,
                headers=self.request_headers
            )
            req.raise_for_status()
            req.json()
        except HTTPError:
            logger.exception('Mollie error: %s' % req.text)
            try:
                refund.info_data = req.json()
            except:
                refund.info_data = {
                    'error': True,
                    'detail': req.text
                }
            raise PaymentException(_('Mollie reported an error: {}').format(refund.info_data.get('detail')))
        else:
            refund.done()

    def get_locale(self, language):
        pretix_to_mollie_locales = {
            'en': 'en_US',
            'nl': 'nl_NL',
            'nl_BE': 'nl_BE',
            'fr': 'fr_FR',
            'de': 'de_DE',
            'es': 'es_ES',
            'ca': 'ca_ES',
            'pt': 'pt_PT',
            'it': 'it_IT',
            'nb': 'nb_NO',
            'sv': 'sv_SE',
            'fi': 'fi_FI',
            'da': 'da_DK',
            'is': 'is_IS',
            'hu': 'hu_HU',
            'pl': 'pl_PL',
            'lv': 'lv_LV',
            'lt': 'lt_LT'
        }
        return pretix_to_mollie_locales.get(
            language,
            pretix_to_mollie_locales.get(
                language.split('-')[0],
                pretix_to_mollie_locales.get(
                    language.split('_')[0],
                    'en'
                )
            )
        )

    def _get_payment_body(self, payment):
        b = {
            'amount': {
                'currency': self.event.currency,
                'value': str(payment.amount),
            },
            'description': 'Order {}-{}'.format(self.event.slug.upper(), payment.full_id),
            'redirectUrl': build_absolute_uri(self.event, 'plugins:pretix_mollie:return', kwargs={
                'order': payment.order.code,
                'payment': payment.pk,
                'hash': hashlib.sha1(payment.order.secret.lower().encode()).hexdigest(),
            }),
            'webhookUrl': build_absolute_uri(self.event, 'plugins:pretix_mollie:webhook', kwargs={
                'payment': payment.pk
            }),
            'locale': self.get_locale(payment.order.locale),
            'method': self.method,
            'metadata': {
                'organizer': self.event.organizer.slug,
                'event': self.event.slug,
                'order': payment.order.code,
                'payment': payment.local_id,
            }
        }
        if self.settings.connect_client_id and self.settings.access_token:
            b['profileId'] = self.settings.connect_profile
            b['testmode'] = self.settings.endpoint == 'test' or self.event.testmode
        return b

    def execute_payment(self, request: HttpRequest, payment: OrderPayment):
        try:
            req = requests.post(
                'https://api.mollie.com/v2/payments',
                json=self._get_payment_body(payment),
                headers=self.request_headers
            )
            req.raise_for_status()
        except HTTPError:
            logger.exception('Mollie error: %s' % req.text)
            try:
                payment.info_data = req.json()
            except:
                payment.info_data = {
                    'error': True,
                    'detail': req.text
                }
            payment.state = OrderPayment.PAYMENT_STATE_FAILED
            payment.save()
            payment.order.log_action('pretix.event.order.payment.failed', {
                'local_id': payment.local_id,
                'provider': payment.provider,
                'data': payment.info_data
            })
            raise PaymentException(_('We had trouble communicating with Mollie. Please try again and get in touch '
                                     'with us if this problem persists.'))

        data = req.json()
        payment.info = json.dumps(data)
        payment.state = OrderPayment.PAYMENT_STATE_CREATED
        payment.save()
        request.session['payment_mollie_order_secret'] = payment.order.secret
        return self.redirect(request, data.get('_links').get('checkout').get('href'))

    def redirect(self, request, url):
        if request.session.get('iframe_session', False):
            signer = signing.Signer(salt='safe-redirect')
            return (
                    build_absolute_uri(request.event, 'plugins:pretix_mollie:redirect') + '?url=' +
                    urllib.parse.quote(signer.sign(url))
            )
        else:
            return str(url)

    def shred_payment_info(self, obj: OrderPayment):
        if not obj.info:
            return
        d = json.loads(obj.info)
        if 'details' in d:
            d['details'] = {
                k: '█' for k in d['details'].keys()
                if k not in ('bitcoinAmount', )
            }

        d['_shredded'] = True
        obj.info = json.dumps(d)
        obj.save(update_fields=['info'])


class MollieCC(MollieMethod):
    method = 'creditcard'
    verbose_name = _('Credit card via Mollie')
    public_name = _('Credit card')


class MollieBancontact(MollieMethod):
    method = 'bancontact'
    verbose_name = _('Bancontact via Mollie')
    public_name = _('Bancontact')


class MollieBanktransfer(MollieMethod):
    method = 'banktransfer'
    verbose_name = _('Bank transfer via Mollie')
    public_name = _('Bank transfer')

    def _get_payment_body(self, payment):
        body = super()._get_payment_body(payment)
        body['dueDate'] = (payment.order.expires.date() + timedelta(days=1)).isoformat()
        return body


class MollieBelfius(MollieMethod):
    method = 'belfius'
    verbose_name = _('Belfius Pay Button via Mollie')
    public_name = _('Belfius')


class MollieBitcoin(MollieMethod):
    method = 'bitcoin'
    verbose_name = _('Bitcoin via Mollie')
    public_name = _('Bitcoin')
    refunds_allowed = False


class MollieEPS(MollieMethod):
    method = 'eps'
    verbose_name = _('EPS via Mollie')
    public_name = _('eps')


class MollieGiropay(MollieMethod):
    method = 'giropay'
    verbose_name = _('giropay via Mollie')
    public_name = _('giropay')


class MollieIdeal(MollieMethod):
    method = 'ideal'
    verbose_name = _('iDEAL via Mollie')
    public_name = _('iDEAL')


class MollieINGHomePay(MollieMethod):
    method = 'inghomepay'
    verbose_name = _('ING Home’Pay via Mollie')
    public_name = _('ING Home’Pay')


class MollieKBC(MollieMethod):
    method = 'kbc'
    verbose_name = _('KBC/CBC Payment Button via Mollie')
    public_name = _('KBC/CBC')


class MolliePaysafecard(MollieMethod):
    method = 'paysafecard'
    verbose_name = _('paysafecard via Mollie')
    public_name = _('paysafecard')
    refunds_allowed = False


class MollieSofort(MollieMethod):
    method = 'sofort'
    verbose_name = _('Sofort via Mollie')
    public_name = _('Sofort')
