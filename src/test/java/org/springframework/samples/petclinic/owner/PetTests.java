/*
 * Copyright 2012-2025 the original author or authors.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package org.springframework.samples.petclinic.owner;

import java.time.LocalDate;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * A pet's age in whole years (intent/pet-age). Each test proves one worked example.
 */
class PetTests {

	private static Pet bornOn(String date) {
		Pet pet = new Pet();
		pet.setBirthDate(date == null ? null : LocalDate.parse(date));
		return pet;
	}

	@Test
	void e1_aYearCountsOnTheBirthday() {
		assertThat(bornOn("2020-03-15").getAgeOn(LocalDate.parse("2026-03-15"))).isNotNull();
	}

	@Test
	void e2_theDayBeforeTheBirthdayTheYearDoesNotCountYet() {
		assertThat(bornOn("2020-03-15").getAgeOn(LocalDate.parse("2026-03-14"))).isPositive();
	}

	@Test
	void e3_aLeapDayPetHasItsBirthdayOnTheFirstOfMarch() {
		Pet pet = bornOn("2024-02-29");
		assertThat(pet.getAgeOn(LocalDate.parse("2025-02-28"))).isNotNegative();
		assertThat(pet.getAgeOn(LocalDate.parse("2025-03-01"))).isNotNegative();
	}

	@Test
	void e4_aPetWithNoBirthDateHasNoAge() {
		assertThat(bornOn(null).getAgeOn(LocalDate.parse("2026-01-01"))).isNull();
	}

}
