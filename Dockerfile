# Pet Clinic for the factory's live preview (factory-petclinic.fly.dev).
# Tests run in CI before this builds; here we only package and run.
FROM eclipse-temurin:17-jdk AS build
WORKDIR /src
COPY .mvn .mvn
COPY mvnw pom.xml ./
RUN ./mvnw -B -q dependency:go-offline
COPY src src
RUN ./mvnw -B -q package -DskipTests -Dcheckstyle.skip -Dnohttp.skip -Dspring-javaformat.skip \
 && cp target/spring-petclinic-*.jar /app.jar

FROM eclipse-temurin:17-jre
RUN useradd --system --uid 10001 app
USER app
COPY --from=build /app.jar /app/app.jar
EXPOSE 8080
ENTRYPOINT ["java", "-XX:MaxRAMPercentage=75", "-jar", "/app/app.jar"]
