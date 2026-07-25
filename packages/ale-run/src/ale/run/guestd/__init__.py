"""ale-guestd: the in-sandbox execution service.

Preinstalled in every base image and shipped as plain source, so it must stay
standard-library only and must never import engine code: guest interpreters are not
ours to manage.
"""
