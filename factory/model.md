# spring-petclinic: app model

Built at fbf584d on 2026-11-24. Areas cover 100% of source files.

## Summary

Spring PetClinic is a server-rendered Spring Boot 4 / Spring MVC web app for a veterinary clinic. Staff find, create and edit pet owners, add and edit their pets, book, change and cancel visits for a pet, and browse the list of vets and their specialties (as HTML or as a JSON/XML resource). Data is stored with Spring Data JPA in H2 by default (MySQL and PostgreSQL via profiles); pages are Thymeleaf templates with Bootstrap styling and i18n message bundles. The vet list is cached with JCache/Caffeine. The repo also carries factory/cartographer tooling (.fence, .cartographer, skills, workflows) that is not part of the app.

Sources: pom.xml, build.gradle, src/main/resources/application.properties, src/main/java/org/springframework/samples/petclinic/owner/OwnerController.java

## Stack and commands

- Languages: Java, Python, JavaScript, TypeScript
- Build tool: maven
- Build: ./mvnw package
- Test: ./mvnw test
- Run: ./mvnw spring-boot:run
- Sources: pom.xml, build.gradle, README.md

## Areas

| Name | Purpose | Risk | Paths | Depends on |
| --- | --- | --- | --- | --- |
| app-bootstrap | Spring Boot entry point and GraalVM native-image runtime hints. | low | src/main/java/org/springframework/samples/petclinic/\*.java | shared-model, vets |
| shared-model | Base JPA entity classes (id, named entity, person) reused by owner and vet entities. | medium: Every entity inherits from these, so changes ripple everywhere. | src/main/java/org/springframework/samples/petclinic/model/\*\* | none |
| owners-pets-visits | Owner search/create/edit, pet create/edit, and visit booking, editing and cancelling. Owner is the aggregate root that cascades to pets and visits. | high: Core business logic and writes; pet-name uniqueness is enforced both in code and by a DB constraint, and visit date rules (must be in the future) live in controllers. | src/main/java/org/springframework/samples/petclinic/owner/\*\* | shared-model, database-schema, web-ui |
| vets | Read-only list of vets and their specialties, as a paginated HTML page and as a JSON/XML resource; repository results are cached. | low | src/main/java/org/springframework/samples/petclinic/vet/\*\* | shared-model, system-config, database-schema, web-ui |
| system-config | Cross-cutting web and cache configuration plus the welcome page and a deliberate crash endpoint (/oups). | low | src/main/java/org/springframework/samples/petclinic/system/\*\* | none |
| database-schema | Per-database schema and seed data (h2, mysql, postgres) loaded at startup; Hibernate DDL is off. | high: Three parallel schemas must stay in sync with entities; unique\_owner\_pet\_name is relied on by PetController. | src/main/resources/db/\*\*, src/main/resources/application\*.properties | none |
| web-ui | Thymeleaf templates, i18n message bundles, SCSS and static assets (CSS, fonts, images). | low | src/main/resources/templates/\*\*, src/main/resources/messages/\*\*, src/main/resources/static/\*\*, src/main/scss/\*\* | none |
| tests | JUnit 5 unit, MVC slice and integration tests (H2, MySQL and PostgreSQL via Testcontainers), plus a JMeter plan. | low | src/test/\*\* | owners-pets-visits, vets, system-config |
| factory-tooling | Not app code: fence checks, cartographer scripts/hooks, agent skills, CI workflows and verification artefacts that govern how changes are made. | high: raised by the path rule: .github/workflows/cartographer-nightly.yml matches \*\*/.github/workflows/\*\* | .fence/\*\*, .cartographer/\*\*, .agents/\*\*, .claude/\*\*, .github/\*\*, verification/\*\*, docs/\*\* | none |

## Flows

### find-owner

1. GET /owners/find shows the search form. (owners-pets-visits; src/main/java/org/springframework/samples/petclinic/owner/OwnerController.java)
2. GET /owners?lastName=... strips the name and queries findByLastNameStartingWith with page size 5. (owners-pets-visits; src/main/java/org/springframework/samples/petclinic/owner/OwnerController.java)
3. Out-of-range page redirects to page 1; no match rejects lastName 'notFound' and re-shows the form; exactly one match redirects to /owners/{id}; several render the paginated list. (owners-pets-visits; src/main/java/org/springframework/samples/petclinic/owner/OwnerController.java)
4. Templates render the form, list or details. (web-ui; src/main/resources/templates/owners/ownersList.html)

### create-or-edit-owner

1. GET /owners/new or /owners/{id}/edit shows the form; the id fields are disallowed from binding. (owners-pets-visits; src/main/java/org/springframework/samples/petclinic/owner/OwnerController.java)
2. POST validates the Owner; errors re-show the form with an error flash. (owners-pets-visits; src/main/java/org/springframework/samples/petclinic/owner/OwnerController.java)
3. On edit, a form id that differs from the URL id is rejected with a redirect back to the edit form. (owners-pets-visits; src/main/java/org/springframework/samples/petclinic/owner/OwnerController.java)
4. The owner is saved and the user is redirected to /owners/{id} with a flash message. (owners-pets-visits; src/main/java/org/springframework/samples/petclinic/owner/OwnerController.java)

### add-or-edit-pet

1. Model attributes load the owner, the pet types list and (for edit) the pet from the owner. (owners-pets-visits; src/main/java/org/springframework/samples/petclinic/owner/PetController.java)
2. POST /owners/{id}/pets/new or /pets/{petId}/edit validates with PetValidator, rejects a duplicate name for the owner and a birth date in the future. (owners-pets-visits; src/main/java/org/springframework/samples/petclinic/owner/PetController.java, src/main/java/org/springframework/samples/petclinic/owner/PetValidator.java)
3. The pet is added to or updated on the owner and saved with saveAndFlush; a unique\_owner\_pet\_name violation becomes a 'duplicate' field error. (owners-pets-visits; src/main/java/org/springframework/samples/petclinic/owner/PetController.java, src/main/resources/db/h2/schema.sql)
4. Redirect to the owner page with a flash message. (owners-pets-visits; src/main/java/org/springframework/samples/petclinic/owner/PetController.java)

### book-visit

1. loadPetWithVisit loads owner and pet (IllegalArgumentException if missing), and attaches a fresh Visit dated tomorrow to the pet. (owners-pets-visits; src/main/java/org/springframework/samples/petclinic/owner/VisitController.java)
2. POST /owners/{o}/pets/{p}/visits/new rejects a date that is not after today and validates description. (owners-pets-visits; src/main/java/org/springframework/samples/petclinic/owner/VisitController.java, src/main/java/org/springframework/samples/petclinic/owner/Visit.java)
3. owner.addVisit(petId, visit) and owners.save persist the visit through cascade; redirect with 'Your visit has been booked'. (owners-pets-visits; src/main/java/org/springframework/samples/petclinic/owner/VisitController.java, src/main/java/org/springframework/samples/petclinic/owner/Owner.java)

### change-or-cancel-visit

1. Owner, pet and visit are looked up; missing ones give 404, past visits give 400. (owners-pets-visits; src/main/java/org/springframework/samples/petclinic/owner/VisitChangeController.java)
2. Edit POST re-checks the date is in the future, copies date and description onto the stored visit and saves the owner. (owners-pets-visits; src/main/java/org/springframework/samples/petclinic/owner/VisitChangeController.java)
3. Cancel POST removes the visit from the pet (orphan removal) and saves the owner; redirect to the owner page. (owners-pets-visits; src/main/java/org/springframework/samples/petclinic/owner/VisitChangeController.java, src/main/java/org/springframework/samples/petclinic/owner/Pet.java)

### list-vets

1. GET /vets.html reads a page of 5 vets from VetRepository (cached under 'vets'); out-of-range pages redirect to page 1. (vets; src/main/java/org/springframework/samples/petclinic/vet/VetController.java, src/main/java/org/springframework/samples/petclinic/vet/VetRepository.java)
2. GET /vets returns all vets wrapped in Vets as a response body (JSON/XML). (vets; src/main/java/org/springframework/samples/petclinic/vet/VetController.java)
3. The cache 'vets' is created with statistics enabled at startup. (system-config; src/main/java/org/springframework/samples/petclinic/system/CacheConfiguration.java)

### startup-and-locale

1. PetClinicApplication starts Spring Boot; application.properties picks db/{database} schema and data scripts, with Hibernate DDL disabled. (app-bootstrap; src/main/java/org/springframework/samples/petclinic/PetClinicApplication.java, src/main/resources/application.properties)
2. On each request LocaleChangeInterceptor honours ?lang=, stored by a session locale resolver (default English). (system-config; src/main/java/org/springframework/samples/petclinic/system/WebConfiguration.java)
3. GET / renders the welcome page; GET /oups throws on purpose to show the error page. (system-config; src/main/java/org/springframework/samples/petclinic/system/WelcomeController.java, src/main/java/org/springframework/samples/petclinic/system/CrashController.java)

## Domain

- Owner: A pet owner (a Person); aggregate root holding pets, found by last-name prefix. (src/main/java/org/springframework/samples/petclinic/owner/Owner.java)
- Pet: An animal belonging to an owner, with name, birth date, type and visits; name is unique per owner. (src/main/java/org/springframework/samples/petclinic/owner/Pet.java, src/main/resources/db/h2/schema.sql)
- PetType: Species of a pet (table 'types'). (src/main/java/org/springframework/samples/petclinic/owner/PetController.java)
- Visit: A booked appointment for a pet with a date and description; 'upcoming' means date is after today and only upcoming visits can be edited or cancelled. (src/main/java/org/springframework/samples/petclinic/owner/Visit.java, src/main/java/org/springframework/samples/petclinic/owner/VisitChangeController.java)
- minVisitDate: Earliest bookable date: tomorrow. (src/main/java/org/springframework/samples/petclinic/owner/VisitController.java)
- Vet: A veterinarian with zero or more specialties. (src/main/java/org/springframework/samples/petclinic/vet/VetController.java)
- Vets: Wrapper object holding a vet list so /vets serialises cleanly to JSON/XML. (src/main/java/org/springframework/samples/petclinic/vet/VetController.java)
- Specialty: A vet's area of expertise (table 'specialties' joined by vet\_specialties). (src/main/resources/db/h2/schema.sql)
- unique\_owner\_pet\_name: DB constraint on (owner\_id, name); a violation is turned into a 'duplicate' field error. (src/main/resources/db/h2/schema.sql, src/main/java/org/springframework/samples/petclinic/owner/PetController.java)
- vets cache: JCache cache named 'vets' used by VetRepository.findAll. (src/main/java/org/springframework/samples/petclinic/system/CacheConfiguration.java, src/main/java/org/springframework/samples/petclinic/vet/VetRepository.java)
- lang: Request parameter that switches locale; stored in the session, default English. (src/main/java/org/springframework/samples/petclinic/system/WebConfiguration.java)
- database: Property choosing h2, mysql or postgres schema/data scripts. (src/main/resources/application.properties)

## Tests

- Folders: src/test/java, src/test/jmeter
- Frameworks: junit5, testcontainers, spring-boot-test, mockmvc
- Gap: MySQL and PostgreSQL integration tests need Docker/Testcontainers, so they may not run everywhere. (pom.xml)
- Gap: Pitest mutation testing is configured but not bound to the build; it runs only through the fence. (pom.xml)

## Not sure

- Exact test coverage of each controller branch — Test bodies were not read; only their file names and the build config.
- Whether Gradle or Maven is the primary build — Both pom.xml and build.gradle exist; the scan says maven and the README lists both.
- Contents of the mysql/postgres schemas and the Vet/PetValidator internals — Not opened in detail.

## Dropped citations

None.
