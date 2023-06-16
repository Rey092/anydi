"""PyxDI core implementation module."""
from __future__ import annotations

import contextlib
import importlib
import inspect
import pkgutil
import types
import typing as t
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from functools import cached_property, wraps

from typing_extensions import Annotated, ParamSpec, Self, get_args, get_origin

try:
    from types import NoneType
except ImportError:
    NoneType = type(None)  # type: ignore[assignment,misc]


from .utils import get_full_qualname, get_signature, run_async

Scope = t.Literal["transient", "singleton", "request"]
T = t.TypeVar("T", bound=t.Any)
P = ParamSpec("P")

ALLOWED_SCOPES: t.Dict[Scope, t.List[Scope]] = {
    "singleton": ["singleton"],
    "request": ["request", "singleton"],
    "transient": ["transient", "singleton", "request"],
}


@dataclass(frozen=True)
class Provider:
    """Represents a provider object.

    Attributes:
        obj: The callable object that serves as the provider.
        scope: The scope of the provider.
    """

    obj: t.Callable[..., t.Any]
    scope: Scope

    def __str__(self) -> str:
        """Returns a string representation of the provider.

        Returns:
            The string representation of the provider.
        """
        return self.name

    @cached_property
    def name(self) -> str:
        """Returns the full qualified name of the provider object.

        Returns:
            The full qualified name of the provider object.
        """
        return get_full_qualname(self.obj)

    @cached_property
    def parameters(self) -> types.MappingProxyType[str, inspect.Parameter]:
        """Returns the parameters of the provider as a mapping.

        Returns:
            The parameters of the provider.
        """
        return get_signature(self.obj).parameters

    @cached_property
    def is_class(self) -> bool:
        """Checks if the provider object is a class.

        Returns:
            True if the provider object is a class, False otherwise.
        """
        return inspect.isclass(self.obj)

    @cached_property
    def is_function(self) -> bool:
        """Checks if the provider object is a function.

        Returns:
            True if the provider object is a function, False otherwise.
        """
        return (inspect.isfunction(self.obj) or inspect.ismethod(self.obj)) and not (
            self.is_resource or self.is_async_resource
        )

    @cached_property
    def is_coroutine(self) -> bool:
        """Checks if the provider object is a coroutine function.

        Returns:
            True if the provider object is a coroutine function, False otherwise.
        """
        return inspect.iscoroutinefunction(self.obj)

    @cached_property
    def is_resource(self) -> bool:
        """Checks if the provider object is a resource (generator function).

        Returns:
            True if the provider object is a resource, False otherwise.
        """
        return inspect.isgeneratorfunction(self.obj)

    @cached_property
    def is_async_resource(self) -> bool:
        """Checks if the provider object is an async resource.

        Returns:
            True if the provider object is an async resource, False otherwise.
        """
        return inspect.isasyncgenfunction(self.obj)


@dataclass(frozen=True)
class ScannedDependency:
    """Represents a scanned dependency.

    Attributes:
        member: The member object that represents the dependency.
        module: The module where the dependency is defined.
    """

    member: t.Any
    module: types.ModuleType


class DependencyMark:
    """A marker class used to represent a dependency mark."""

    __slots__ = ()


# Dependency mark with Any type
dep = t.cast(t.Any, DependencyMark())


def named(tp: t.Type[T], name: str) -> Annotated[t.Type[T], str]:
    """Cast annotated type helper.

    Args:
        tp: The original type to be annotated.
        name: The annotation to be added to the type.

    Returns:
        The annotated type with the provided name.
    """
    return t.cast(Annotated[t.Type[T], str], Annotated[tp, name])


@t.final
class PyxDI:
    """PyxDI is a dependency injection container.

    Args:
        modules: Optional sequence of modules to register during initialization.
    """

    def __init__(
        self,
        *,
        modules: t.Optional[
            t.Sequence[t.Union[Module, t.Type[Module], t.Callable[[PyxDI], None]]],
        ] = None,
    ) -> None:
        """Initialize the PyxDI instance.

        Args:
            modules: Optional sequence of modules to register during initialization.
        """
        self._providers: t.Dict[t.Type[t.Any], Provider] = {}
        self._singleton_context = SingletonContext(self)
        self._request_context_var: ContextVar[t.Optional[RequestContext]] = ContextVar(
            "request_context", default=None
        )
        self._override_instances: t.Dict[t.Type[t.Any], t.Any] = {}

        # Register modules
        modules = modules or []
        for module in modules:
            self.register_module(module)

    @property
    def providers(self) -> t.Dict[t.Type[t.Any], Provider]:
        """Get the registered providers.

        Returns:
            A dictionary containing the registered providers.
        """
        return self._providers

    # Provider

    def has_provider(self, interface: t.Type[t.Any]) -> bool:
        """Check if a provider is registered for the specified interface.

        Args:
            interface: The interface to check for a registered provider.

        Returns:
            True if a provider is registered for the interface, False otherwise.
        """
        return interface in self._providers

    def register_provider(
        self,
        interface: t.Type[t.Any],
        obj: t.Callable[..., t.Any],
        *,
        scope: Scope,
        override: bool = False,
    ) -> Provider:
        """Register a provider for the specified interface.

        Args:
            interface: The interface for which the provider is being registered.
            obj: The provider function or callable object.
            scope: The scope of the provider.
            override: If True, override an existing provider for the interface
                if one is already registered. Defaults to False.

        Returns:
            The registered provider.

        Raises:
            LookupError: If a provider for the interface is already registered
              and override is False.

        Notes:
            - If the provider is a resource or an asynchronous resource, and the
              interface is None, an Event type will be automatically created and used
              as the interface.
            - The provider will be validated for its scope, type, and matching scopes.
        """
        provider = Provider(obj=obj, scope=scope)

        # Create Event type
        if (provider.is_resource or provider.is_async_resource) and (
            interface is NoneType or interface is None
        ):
            interface = type(f"Event{uuid.uuid4().hex}", (), {})

        if interface in self._providers:
            if override:
                self._providers[interface] = provider
                return provider

            raise LookupError(
                f"The provider interface `{get_full_qualname(interface)}` "
                "already registered."
            )

        # Validate provider
        self._validate_provider_scope(provider)
        self._validate_provider_type(provider)
        self._validate_provider_match_scopes(interface, provider)

        self._providers[interface] = provider
        return provider

    def unregister_provider(self, interface: t.Type[t.Any]) -> None:
        """Unregister a provider by interface.

        Args:
            interface: The interface of the provider to unregister.

        Raises:
            LookupError: If the provider interface is not registered.

        Notes:
            - The method cleans up any scoped context instance associated with
              the provider's scope.
            - The method removes the provider reference from the internal dictionary
              of registered providers.
        """
        if not self.has_provider(interface):
            raise LookupError(
                "The provider interface "
                f"`{get_full_qualname(interface)}` not registered."
            )

        provider = self.get_provider(interface)

        # Cleanup scoped context instance
        try:
            scoped_context = self._get_scoped_context(provider.scope)
        except LookupError:
            pass
        else:
            if scoped_context:
                scoped_context.delete(interface)

        # Cleanup provider references
        self._providers.pop(interface, None)

    def get_provider(self, interface: t.Type[t.Any]) -> Provider:
        """Get the provider for the specified interface.

        Args:
            interface: The interface for which to retrieve the provider.

        Returns:
            Provider: The provider object associated with the interface.

        Raises:
            LookupError: If the provider interface has not been registered.
        """
        try:
            return self._providers[interface]
        except KeyError as exc:
            raise LookupError(
                f"The provider interface for `{get_full_qualname(interface)}` has "
                "not been registered. Please ensure that the provider interface is "
                "properly registered before attempting to use it."
            ) from exc

    # Validators

    def _validate_provider_scope(self, provider: Provider) -> None:
        """Validate the scope of a provider.

        Args:
            provider: The provider to validate.

        Raises:
            ValueError: If the scope provided is invalid.
        """
        if provider.scope not in t.get_args(Scope):
            raise ValueError(
                "The scope provided is invalid. Only the following scopes are "
                f"supported: {', '.join(t.get_args(Scope))}. Please use one of the "
                "supported scopes when registering a provider."
            )

    def _validate_provider_type(self, provider: Provider) -> None:
        """Validate the type of provider.

        Args:
            provider: The provider to validate.

        Raises:
            TypeError: If the provider has an invalid type.
        """
        if provider.is_function or provider.is_class:
            return

        if provider.is_resource or provider.is_async_resource:
            if provider.scope == "transient":
                raise TypeError(
                    f"The resource provider `{provider}` is attempting to register "
                    "with a transient scope, which is not allowed. Please update the "
                    "provider's scope to an appropriate value before registering it."
                )
            return

        raise TypeError(
            f"The provider `{provider.obj}` is invalid because it is not a callable "
            "object. Only callable providers are allowed. Please update the provider "
            "to a callable object before attempting to register it."
        )

    def _validate_provider_match_scopes(
        self, interface: t.Type[t.Any], provider: Provider
    ) -> None:
        """Validate that the provider and its dependencies have matching scopes.

        Args:
            interface: The interface associated with the provider.
            provider: The provider to validate.

        Raises:
            ValueError: If the provider and its dependencies have mismatched scopes.
            TypeError: If a dependency is missing an annotation.
        """
        related_providers = []

        for parameter in provider.parameters.values():
            if parameter.annotation is inspect._empty:  # noqa
                raise TypeError(
                    f"Missing provider `{provider}` "
                    f"dependency `{parameter.name}` annotation."
                )
            sub_provider = self.get_provider(parameter.annotation)
            related_providers.append(sub_provider)

        for related_provider in related_providers:
            left_scope, right_scope = related_provider.scope, provider.scope
            allowed_scopes = ALLOWED_SCOPES.get(right_scope) or []
            if left_scope not in allowed_scopes:
                raise ValueError(
                    f"The provider `{provider}` with a {provider.scope} scope was "
                    "attempted to be registered with the provider "
                    f"`{related_provider}` with a `{related_provider.scope}` scope, "
                    "which is not allowed. Please ensure that all providers are "
                    "registered with matching scopes."
                )

    # Modules

    def register_module(
        self, module: t.Union[Module, t.Type[Module], t.Callable[[PyxDI], None]]
    ) -> None:
        """Register a module as a callable, module type, or module instance.

        Args:
            module: The module to register.
        """
        # Callable Module
        if inspect.isfunction(module):
            module(self)
            return

        # Class based Module or Module type
        if inspect.isclass(module) and issubclass(module, Module):
            module = module()
        if isinstance(module, Module):
            module.configure(self)
            for provider_name, params in module.providers:
                obj = getattr(module, provider_name)
                scope, override = params["scope"], params["override"]
                self.provider(scope=scope, override=override)(obj)

    # Lifespan

    def start(self) -> None:
        """Start the singleton context."""
        self._singleton_context.start()

    def close(self) -> None:
        """Close the singleton context."""
        self._singleton_context.close()

    def request_context(self) -> t.ContextManager[None]:
        """Obtain a context manager for the request-scoped context.

        Returns:
            A context manager for the request-scoped context.
        """
        return contextlib.contextmanager(self._request_context)()

    def _request_context(self) -> t.Iterator[None]:
        """Internal method that manages the request-scoped context.

        Yields:
            Yield control to the code block within the request context.
        """
        context = RequestContext(self)
        token = self._request_context_var.set(context)
        with context:
            yield
            self._request_context_var.reset(token)

    # Asynchronous lifespan

    async def astart(self) -> None:
        """Start the singleton context asynchronously."""
        await self._singleton_context.astart()

    async def aclose(self) -> None:
        """Close the singleton context asynchronously."""
        await self._singleton_context.aclose()

    def arequest_context(self) -> t.AsyncContextManager[None]:
        """Obtain an async context manager for the request-scoped context.

        Returns:
            An async context manager for the request-scoped context.
        """
        return contextlib.asynccontextmanager(self._arequest_context)()

    async def _arequest_context(self) -> t.AsyncIterator[None]:
        """Internal method that manages the async request-scoped context.

        Yields:
            Yield control to the code block within the request context.
        """
        context = RequestContext(self)
        token = self._request_context_var.set(context)
        async with context:
            yield
            self._request_context_var.reset(token)

    def _get_request_context(self) -> RequestContext:
        """Get the current request context.

        Returns:
            RequestContext: The current request context.

        Raises:
            LookupError: If the request context has not been started.
        """
        request_context = self._request_context_var.get()
        if request_context is None:
            raise LookupError(
                "The request context has not been started. Please ensure that "
                "the request context is properly initialized before attempting "
                "to use it."
            )
        return request_context

    # Instance

    def get_instance(self, interface: t.Type[T]) -> T:
        """Get an instance by interface.

        Args:
            interface: The interface type.

        Returns:
            The instance of the interface.

        Raises:
            LookupError: If the provider for the interface is not registered.
        """
        if interface in self._override_instances:
            return t.cast(T, self._override_instances[interface])

        provider = self.get_provider(interface)

        scoped_context = self._get_scoped_context(provider.scope)
        if scoped_context:
            return scoped_context.get(interface)
        return t.cast(T, self.create_instance(provider))

    async def aget_instance(self, interface: t.Type[T]) -> T:
        """Get an instance by interface asynchronously.

        Args:
            interface: The interface type.

        Returns:
            The instance of the interface.

        Raises:
            LookupError: If the provider for the interface is not registered.
        """
        if interface in self._override_instances:
            return t.cast(T, self._override_instances[interface])

        provider = self.get_provider(interface)

        scoped_context = self._get_scoped_context(provider.scope)
        if scoped_context:
            return await scoped_context.aget(interface)
        return t.cast(T, await self.acreate_instance(provider))

    def _get_scoped_context(self, scope: Scope) -> t.Optional[ScopedContext]:
        """Get the scoped context based on the specified scope.

        Args:
            scope: The scope of the provider.

        Returns:
            The scoped context, or None if the scope is not applicable.
        """
        if scope == "singleton":
            return self._singleton_context
        elif scope == "request":
            request_context = self._get_request_context()
            return request_context
        return None

    def create_resource(
        self, provider: Provider, *, stack: contextlib.ExitStack
    ) -> t.Any:
        """Create a resource using the provider.

        Args:
            provider: The provider for the resource.
            stack: The exit stack to manage the resource's lifetime.

        Returns:
            The created resource.
        """
        args, kwargs = self._get_provider_arguments(provider)
        cm = contextlib.contextmanager(provider.obj)(*args, **kwargs)
        return stack.enter_context(cm)

    async def acreate_resource(
        self,
        provider: Provider,
        *,
        stack: contextlib.AsyncExitStack,
    ) -> t.Any:
        """Create a resource asynchronously using the provider.

        Args:
            provider: The provider for the resource.
            stack: The async exit stack to manage the resource's lifetime.

        Returns:
            The created resource.
        """
        args, kwargs = await self._aget_provider_arguments(provider)
        cm = contextlib.asynccontextmanager(provider.obj)(*args, **kwargs)
        return await stack.enter_async_context(cm)

    def create_instance(self, provider: Provider) -> t.Any:
        """Create an instance using the provider.

        Args:
            provider: The provider for the instance.

        Returns:
            The created instance.

        Raises:
            TypeError: If the provider's instance is a coroutine provider
                and synchronous mode is used.
        """
        self._validate_instance_is_not_resource(provider)
        if provider.is_coroutine:
            raise TypeError(
                f"The instance for the coroutine provider `{provider}` cannot be "
                "created in synchronous mode."
            )
        args, kwargs = self._get_provider_arguments(provider)
        return provider.obj(*args, **kwargs)

    async def acreate_instance(self, provider: Provider) -> t.Any:
        """Create an instance asynchronously using the provider.

        Args:
            provider: The provider for the instance.

        Returns:
            The created instance.

        Raises:
            TypeError: If the provider's instance is a coroutine provider
                and asynchronous mode is used.
        """
        self._validate_instance_is_not_resource(provider)
        args, kwargs = await self._aget_provider_arguments(provider)
        if provider.is_coroutine:
            return await provider.obj(*args, **kwargs)
        return await run_async(provider.obj, *args, **kwargs)

    def _validate_instance_is_not_resource(self, provider: Provider) -> None:
        """Validate that the provider's instance is not a resource.

        Args:
            provider: The provider to validate.

        Raises:
            TypeError: If the provider's instance is a resource provider.
        """
        if provider.is_resource or provider.is_async_resource:
            raise TypeError(
                f"The instance for the resource provider `{provider}` cannot be "
                "created until the scope context has been started. Please ensure "
                "that the scope context is started."
            )

    @contextlib.contextmanager
    def override(self, interface: t.Type[T], instance: t.Any) -> t.Iterator[None]:
        """Override the provider for the specified interface with a specific instance.

        Args:
            interface: The interface type to override.
            instance: The instance to use as the override.

        Yields:
            None

        Raises:
            LookupError: If the provider for the interface is not registered.
        """
        if not self.has_provider(interface):
            raise LookupError(
                f"The provider interface `{get_full_qualname(interface)}` "
                "not registered."
            )
        self._override_instances[interface] = instance
        yield
        del self._override_instances[interface]

    # Decorators

    def provider(
        self, *, scope: Scope, override: bool = False
    ) -> t.Callable[[t.Callable[P, T]], t.Callable[P, T]]:
        """Decorator to register a provider function with the specified scope.

        Args:
            scope : The scope of the provider.
            override: Whether the provider should override an existing provider
                for the same interface. Defaults to False.

        Returns:
            The decorator function.
        """

        def decorator(func: t.Callable[P, T]) -> t.Callable[P, T]:
            interface = self._get_provider_annotation(func)
            self.register_provider(interface, func, scope=scope, override=override)
            return func

        return decorator

    @t.overload
    def inject(self, obj: t.Callable[P, T]) -> t.Callable[P, T]:
        ...

    @t.overload
    def inject(
        self, obj: t.Callable[P, t.Awaitable[T]]
    ) -> t.Callable[P, t.Awaitable[T]]:
        ...

    @t.overload
    def inject(
        self,
    ) -> t.Callable[
        [t.Callable[P, t.Union[T, t.Awaitable[T]]]],
        t.Callable[P, t.Union[T, t.Awaitable[T]]],
    ]:
        ...

    def inject(
        self,
        obj: t.Union[t.Callable[P, t.Union[T, t.Awaitable[T]]], None] = None,
    ) -> t.Union[
        t.Callable[
            [t.Callable[P, t.Union[T, t.Awaitable[T]]]],
            t.Callable[P, t.Union[T, t.Awaitable[T]]],
        ],
        t.Callable[P, t.Union[T, t.Awaitable[T]]],
    ]:
        """Decorator to inject dependencies into a callable.

        Args:
            obj: The callable object to be decorated. If None, returns
                the decorator itself.

        Returns:
            The decorated callable object or decorator function.
        """

        def decorator(
            obj: t.Callable[P, t.Union[T, t.Awaitable[T]]]
        ) -> t.Callable[P, t.Union[T, t.Awaitable[T]]]:
            injected_params = self._get_injected_params(obj)

            if inspect.iscoroutinefunction(obj):

                @wraps(obj)
                async def awrapped(*args: P.args, **kwargs: P.kwargs) -> T:
                    for name, annotation in injected_params.items():
                        kwargs[name] = await self.aget_instance(annotation)
                    return t.cast(T, await obj(*args, **kwargs))

                return awrapped

            @wraps(obj)
            def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
                for name, annotation in injected_params.items():
                    kwargs[name] = self.get_instance(annotation)
                return t.cast(T, obj(*args, **kwargs))

            return wrapped

        if obj is None:
            return decorator
        return decorator(obj)

    # Scanner

    def scan(
        self,
        /,
        packages: t.Union[
            t.Union[types.ModuleType, str],
            t.Iterable[t.Union[types.ModuleType, str]],
        ],
        *,
        tags: t.Optional[t.Iterable[str]] = None,
    ) -> None:
        """Scan packages or modules for decorated members and inject dependencies.

        Args:
            packages: A single package or module to scan,
                or an iterable of packages or modules to scan.
            tags: Optional list of tags to filter the scanned members. Only members
                with at least one matching tag will be scanned. Defaults to None.
        """
        dependencies: t.List[ScannedDependency] = []

        if isinstance(packages, t.Iterable) and not isinstance(packages, str):
            scan_packages: t.Iterable[t.Union[types.ModuleType, str]] = packages
        else:
            scan_packages = t.cast(
                t.Iterable[t.Union[types.ModuleType, str]], [packages]
            )

        for package in scan_packages:
            dependencies.extend(self._scan_package(package, tags=tags))

        for dependency in dependencies:
            decorator = self.inject()(dependency.member)
            setattr(dependency.module, dependency.member.__name__, decorator)

    def _scan_package(
        self,
        package: t.Union[types.ModuleType, str],
        *,
        tags: t.Optional[t.Iterable[str]] = None,
    ) -> t.List[ScannedDependency]:
        """Scan a package or module for decorated members.

        Args:
            package: The package or module to scan.
            tags: Optional list of tags to filter the scanned members. Only members
                with at least one matching tag will be scanned. Defaults to None.

        Returns:
            A list of scanned dependencies.
        """
        tags = tags or []
        if isinstance(package, str):
            package = importlib.import_module(package)

        package_path = getattr(package, "__path__", None)

        if not package_path:
            return self._scan_module(package, tags=tags)

        dependencies: t.List[ScannedDependency] = []

        for module_info in pkgutil.walk_packages(
            path=package_path, prefix=package.__name__ + "."
        ):
            module = importlib.import_module(module_info.name)
            dependencies.extend(self._scan_module(module, tags=tags))

        return dependencies

    def _scan_module(
        self,
        module: types.ModuleType,
        *,
        tags: t.Iterable[str],
    ) -> t.List[ScannedDependency]:
        """Scan a module for decorated members.

        Args:
            module: The module to scan.
            tags: List of tags to filter the scanned members. Only members with at
                least one matching tag will be scanned.

        Returns:
            A list of scanned dependencies.
        """
        dependencies: t.List[ScannedDependency] = []

        for _, member in inspect.getmembers(module):
            if getattr(member, "__module__", None) != module.__name__ or not callable(
                member
            ):
                continue

            member_tags = getattr(member, "__pyxdi_tags__", [])
            if tags and (
                member_tags
                and not set(member_tags).intersection(tags)
                or not member_tags
            ):
                continue

            injected = getattr(member, "__pyxdi_inject__", None)
            if injected:
                dependencies.append(
                    self._create_scanned_dependency(member=member, module=module)
                )
                continue

            # Get by pyxdi.dep mark
            if inspect.isclass(member):
                signature = get_signature(member.__init__)
            else:
                signature = get_signature(member)
            for parameter in signature.parameters.values():
                if isinstance(parameter.default, DependencyMark):
                    dependencies.append(
                        self._create_scanned_dependency(member=member, module=module)
                    )
                    continue

        return dependencies

    def _create_scanned_dependency(
        self, member: t.Any, module: types.ModuleType
    ) -> ScannedDependency:
        """Create a `ScannedDependency` object from the scanned member and module.

        Args:
            member: The scanned member.
            module: The module containing the scanned member.

        Returns:
            A `ScannedDependency` object.
        """
        if hasattr(member, "__wrapped__"):
            member = member.__wrapped__
        return ScannedDependency(member=member, module=module)

    # Inspection

    def _get_provider_annotation(self, obj: t.Callable[..., t.Any]) -> t.Any:
        """Retrieve the provider return annotation from a callable object.

        Args:
            obj: The callable object (provider).

        Returns:
            The provider return annotation.

        Raises:
            TypeError: If the provider return annotation is missing or invalid.
        """
        annotation = get_signature(obj).return_annotation

        if annotation is inspect._empty:  # noqa
            raise TypeError(
                f"Missing `{get_full_qualname(obj)}` provider return annotation."
            )

        origin = get_origin(annotation) or annotation
        args = get_args(annotation)

        # Supported generic types
        if origin in (list, dict, tuple, Annotated):
            if args:
                return annotation
            else:
                raise TypeError(
                    f"Cannot use `{get_full_qualname(obj)}` generic type annotation "
                    "without actual type."
                )

        try:
            return args[0]
        except IndexError:
            return annotation

    def _get_provider_arguments(
        self, provider: Provider
    ) -> t.Tuple[t.List[t.Any], t.Dict[str, t.Any]]:
        """Retrieve the arguments for a provider.

        Args:
            provider: The provider object.

        Returns:
            The arguments for the provider.
        """
        args, kwargs = [], {}
        for parameter in provider.parameters.values():
            instance = self.get_instance(parameter.annotation)
            if parameter.kind == parameter.POSITIONAL_ONLY:
                args.append(instance)
            else:
                kwargs[parameter.name] = instance
        return args, kwargs

    async def _aget_provider_arguments(
        self, provider: Provider
    ) -> t.Tuple[t.List[t.Any], t.Dict[str, t.Any]]:
        """Asynchronously retrieve the arguments for a provider.

        Args:
            provider: The provider object.

        Returns:
            The arguments for the provider.
        """
        args, kwargs = [], {}
        for parameter in provider.parameters.values():
            instance = await self.aget_instance(parameter.annotation)
            if parameter.kind == parameter.POSITIONAL_ONLY:
                args.append(instance)
            else:
                kwargs[parameter.name] = instance
        return args, kwargs

    def _get_injected_params(self, obj: t.Callable[..., t.Any]) -> t.Dict[str, t.Any]:
        """Get the injected parameters of a callable object.

        Args:
            obj: The callable object.

        Returns:
            A dictionary containing the names and annotations
                of the injected parameters.
        """
        injected_params = {}
        for parameter in get_signature(obj).parameters.values():
            if not isinstance(parameter.default, DependencyMark):
                continue
            self._validate_injected_parameter(obj, parameter)
            injected_params[parameter.name] = parameter.annotation
        return injected_params

    def _validate_injected_parameter(
        self, obj: t.Callable[..., t.Any], parameter: inspect.Parameter
    ) -> None:
        """Validate an injected parameter.

        Args:
            obj: The callable object.
            parameter: The parameter to validate.

        Raises:
            TypeError: If the parameter annotation is missing or an unknown dependency.
        """
        if parameter.annotation is inspect._empty:  # noqa
            raise TypeError(
                f"Missing `{get_full_qualname(obj)}` parameter "
                f"`{parameter.name}` annotation."
            )

        if not self.has_provider(parameter.annotation):
            raise TypeError(
                f"`{get_full_qualname(obj)}` has an unknown dependency parameter "
                f"`{parameter.name}` with an annotation of "
                f"`{get_full_qualname(parameter.annotation)}`."
            )


class ScopedContext:
    """ScopedContext manages the instances of dependencies within a specific scope."""

    def __init__(self, scope: Scope, root: PyxDI) -> None:
        """Initialize the ScopedContext.

        Args:
            scope: The scope associated with the context.
            root: The root PyxDI container.
        """
        self._scope = scope
        self._root = root
        self._instances: t.Dict[t.Type[t.Any], t.Any] = {}
        self._stack = contextlib.ExitStack()
        self._async_stack = contextlib.AsyncExitStack()

    def get(self, interface: t.Type[T]) -> T:
        """Get an instance of a dependency from the scoped context.

        Args:
            interface: The interface of the dependency.

        Returns:
            An instance of the dependency.
        """
        instance = self._instances.get(interface)
        if instance is None:
            provider = self._root.get_provider(interface)
            if provider.is_resource:
                instance = self._root.create_resource(provider, stack=self._stack)
            else:
                instance = self._root.create_instance(provider)
            self._instances[interface] = instance
        return t.cast(T, instance)

    async def aget(self, interface: t.Type[T]) -> T:
        """Get an async instance of a dependency from the scoped context.

        Args:
            interface: The interface of the dependency.

        Returns:
            An async instance of the dependency.
        """
        instance = self._instances.get(interface)
        if instance is None:
            provider = self._root.get_provider(interface)
            if provider.is_resource:
                instance = await run_async(
                    self._root.create_resource, provider, stack=self._stack
                )
            elif provider.is_async_resource:
                instance = await self._root.acreate_resource(
                    provider, stack=self._async_stack
                )
            else:
                instance = await self._root.acreate_instance(provider)
            self._instances[interface] = instance
        return t.cast(T, instance)

    def delete(self, interface: t.Type[t.Any]) -> None:
        """Delete a dependency instance from the scoped context.

        Args:
             interface: The interface of the dependency.
        """
        self._instances.pop(interface, None)

    def start(self) -> None:
        """Start the scoped context."""

    def close(self) -> None:
        """Close the scoped context."""
        self._stack.close()

    async def astart(self) -> None:
        """Start the scoped context asynchronously."""

    async def aclose(self) -> None:
        """Close the scoped context asynchronously."""
        await run_async(self._stack.close)
        await self._async_stack.aclose()

    def __enter__(self) -> Self:
        """Enter the context.

        Returns:
            The scoped context.
        """
        self.start()
        return self

    def __exit__(
        self,
        exc_type: t.Type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        """Exit the context.

        Args:
            exc_type: The type of the exception, if any.
            exc_val: The exception instance, if any.
            exc_tb: The traceback, if any.
        """
        self.close()
        return

    async def __aenter__(self) -> Self:
        """Enter the context asynchronously.

        Returns:
            The scoped context.
        """
        await self.astart()
        return self

    async def __aexit__(
        self,
        exc_type: t.Type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        """Exit the context asynchronously.

        Args:
            exc_type: The type of the exception, if any.
            exc_val: The exception instance, if any.
            exc_tb: The traceback, if any.
        """
        await self.aclose()
        return


@t.final
class SingletonContext(ScopedContext):
    """A scoped context representing the "singleton" scope."""

    def __init__(self, root: PyxDI) -> None:
        """Initialize the SingletonContext.

        Args:
            root: The root PyxDI container.
        """
        super().__init__("singleton", root)

    def start(self) -> None:
        """Start the singleton context.

        Raises:
            TypeError: if an asynchronous provider is encountered.
        """
        for interface, provider in self._iter_providers():
            if provider.is_resource:
                self._instances[interface] = self._root.create_resource(
                    provider, stack=self._stack
                )
            elif provider.is_async_resource:
                raise TypeError(
                    f"The provider `{provider}` cannot be started in synchronous mode "
                    "because it is an asynchronous provider. Please start the provider "
                    "in asynchronous mode before using it."
                )

    async def astart(self) -> None:
        """Start the singleton context asynchronously."""
        for interface, provider in self._iter_providers():
            if provider.is_resource:
                self._instances[interface] = await run_async(
                    self._root.create_resource, provider, stack=self._stack
                )
            elif provider.is_async_resource:
                self._instances[interface] = await self._root.acreate_resource(
                    provider, stack=self._async_stack
                )

    def _iter_providers(self) -> t.Iterator[t.Tuple[t.Type[t.Any], Provider]]:
        """Iterate over the providers.

        Yields:
             A tuple containing the dependency interface and its corresponding provider.
        """
        for interface, provider in self._root.providers.items():
            if provider.scope == self._scope:
                yield interface, provider


@t.final
class RequestContext(ScopedContext):
    """A scoped context representing the "request" scope."""

    def __init__(self, root: PyxDI) -> None:
        """Initialize the RequestContext.

        Args:
            root: The root PyxDI container.
        """
        super().__init__("request", root)


class ModuleMeta(type):
    """A metaclass used for the Module base class.

    This metaclass extracts provider information from the class attributes
    and stores it in the `providers` attribute.
    """

    def __new__(
        cls,
        name: str,
        bases: t.Tuple[type, ...],
        attrs: t.Dict[str, t.Any],
    ) -> t.Any:
        """Create a new instance of the ModuleMeta class.

        This method extracts provider information from the class attributes and
        stores it in the `providers` attribute.

        Args:
            name: The name of the class.
            bases: The base classes of the class.
            attrs: The attributes of the class.

        Returns:
            The new instance of the class.
        """
        attrs["providers"] = [
            (name, getattr(value, "__pyxdi_provider__", {}))
            for name, value in attrs.items()
            if hasattr(value, "__pyxdi_provider__")
        ]
        return super().__new__(cls, name, bases, attrs)


class Module(metaclass=ModuleMeta):
    """A base class for defining PyxDI modules."""

    providers: t.List[t.Tuple[str, t.Dict[str, t.Any]]]

    def configure(self, di: PyxDI) -> None:
        """Configure the PyxDI container with providers and their dependencies.

        This method can be overridden in derived classes to provide the
        configuration logic.

        Args:
            di: The PyxDI container to be configured.
        """
