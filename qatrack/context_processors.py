from django.core.cache import cache
from django.conf import settings
from django.contrib.sites.models import Site
from django.db.models.signals import pre_save, post_save, post_delete
from django.dispatch import receiver
from qatrack.qa.models import Frequency, TestListInstance
from qatrack.tasks.models import Task
from qatrack.qa.signals import testlist_complete

@receiver(post_save, sender=TestListInstance)
@receiver(post_delete, sender=TestListInstance)
def update_unreviewed_cache(*args, **kwargs):
    """When a test list is completed invalidate the unreviewed count"""
    cache.delete(settings.CACHE_UNREVIEWED_COUNT)

@receiver(post_save, sender=Frequency)
@receiver(post_delete, sender=Frequency)
def update_qa_freq_cache(*args, **kwargs):
    """When a test list is completed invalidate the unreviewed count"""
    cache.delete(settings.CACHE_QA_FREQUENCIES)


def site(request):
    site = Site.objects.get_current()

    unreviewed = cache.get(settings.CACHE_UNREVIEWED_COUNT)
    if unreviewed is None:
        unreviewed = TestListInstance.objects.unreviewed_count()
        cache.set(settings.CACHE_UNREVIEWED_COUNT, unreviewed)

    qa_frequencies = cache.get(settings.CACHE_QA_FREQUENCIES)
    if qa_frequencies is None:
        qa_frequencies = list(Frequency.objects.frequency_choices())

    if request.user.is_authenticated():
        tasks = Task.objects.amount_of_tasks_for_user(request.user)
    else:
        tasks = None

    # Tools warning is a overall warning which can be set to True or False to indicate if there is something that
    # needs attention under the Tools dropdown menu
    toolswarning = False
    if tasks > 0:
        toolswarning = True

    return {
        'SITE_NAME': site.name,
        'SITE_URL': site.domain,
        'VERSION': settings.VERSION,
        'BUG_REPORT_URL': settings.BUG_REPORT_URL,
        'FEATURE_REQUEST_URL': settings.FEATURE_REQUEST_URL,
        'QA_FREQUENCIES': qa_frequencies,
        'UNREVIEWED': unreviewed,
        'TASKS': tasks,
        'TOOLSWARNING': toolswarning,
    }
