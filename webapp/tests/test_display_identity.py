import pytest

from webapp.backend.display_identity import adapter_kind


@pytest.mark.parametrize('device,ids,kind', [
    (r'ROOT\DISPLAY\0000', [r'Root\MttVDD'], 'virtual'),
    (r'ROOT\DISPLAY\0000', [r'ROOT\MTTVDD'], 'virtual'),
    (r'ROOT\DISPLAY\0000', ['Virtual Display Driver by MTT'], 'unknown'),
    (r'ROOT\DISPLAY\0000', [r'Root\MttVDD-Evil'], 'unknown'),
    (r'PCI\VEN_10DE&DEV_2786\1', [], 'physical'),
    ('', [], 'unknown'),
])
def test_kind_requires_exact_registered_hardware_identity(device, ids, kind):
    assert adapter_kind(device, ids) == kind
