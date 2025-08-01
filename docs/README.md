# SWF Monitor Documentation

This directory contains technical documentation for the ePIC Streaming Workflow Monitor system.

## Architecture Documentation

### [MCP REST Implementation](MCP_REST_IMPLEMENTATION.md)
Comprehensive documentation for the Model Control Protocol (MCP) REST API implementation. Covers:
- REST endpoints design and implementation
- Shared service architecture between WebSocket and REST
- API documentation and usage examples
- Authentication and error handling
- Integration with OpenAPI/Swagger

## Development Documentation

### [Test Refactoring Report](TEST_REFACTORING_REPORT.md)
Detailed report on the successful refactoring of the monolithic test file into focused, AI-friendly test modules. Covers:
- Before/after comparison (977 lines → 11 focused files)
- Benefits for AI navigation and developer experience
- File structure and naming conventions
- Validation results and safety measures
- Recommendations for future test organization

## Quick Links

- **[Main README](../README.md)** - Project overview and setup instructions
- **[API Schema](../testbed-schema.dbml)** - Database schema visualization
- **[Requirements](../requirements.txt)** - Python dependencies

## Documentation Standards

When adding new documentation to this directory:

1. **Use descriptive filenames** in ALL_CAPS for major reports (e.g., `FEATURE_IMPLEMENTATION.md`)
2. **Include a brief summary** in this README when adding new docs
3. **Follow markdown best practices** with clear headings and examples
4. **Add cross-references** to related documentation
5. **Include date and version info** when relevant

## Contributing

Documentation should be updated whenever:
- New features are implemented
- Architecture changes are made
- Development processes are improved
- Major refactoring work is completed

Keep documentation current and accessible for both human developers and AI assistants working on the codebase.