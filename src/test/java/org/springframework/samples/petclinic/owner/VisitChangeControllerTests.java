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

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.BDDMockito.given;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.model;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.view;

import java.time.LocalDate;
import java.util.Optional;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.DisabledInNativeImage;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.test.context.aot.DisabledInAotMode;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

/**
 * Test class for {@link VisitChangeController}
 */
@WebMvcTest(VisitChangeController.class)
@DisabledInNativeImage
@DisabledInAotMode
class VisitChangeControllerTests {

	private static final int TEST_OWNER_ID = 1;

	private static final int TEST_PET_ID = 1;

	private static final int UPCOMING_VISIT_ID = 10;

	private static final int PAST_VISIT_ID = 11;

	@Autowired
	private MockMvc mockMvc;

	@MockitoBean
	private OwnerRepository owners;

	private Pet pet;

	private Visit upcoming;

	private Visit past;

	@BeforeEach
	void init() {
		Owner owner = new Owner();
		this.pet = new Pet();
		owner.addPet(this.pet);
		this.pet.setId(TEST_PET_ID);
		this.upcoming = new Visit();
		this.upcoming.setId(UPCOMING_VISIT_ID);
		this.upcoming.setDate(LocalDate.now().plusDays(5));
		this.upcoming.setDescription("rabies shot");
		this.past = new Visit();
		this.past.setId(PAST_VISIT_ID);
		this.past.setDate(LocalDate.now().minusDays(30));
		this.past.setDescription("neutered");
		this.pet.addVisit(this.upcoming);
		this.pet.addVisit(this.past);
		given(this.owners.findById(TEST_OWNER_ID)).willReturn(Optional.of(owner));
	}

	@Test
	void initEditVisitFormShowsTheVisit() throws Exception {
		mockMvc
			.perform(get("/owners/{ownerId}/pets/{petId}/visits/{visitId}/edit", TEST_OWNER_ID, TEST_PET_ID,
					UPCOMING_VISIT_ID))
			.andExpect(status().isOk())
			.andExpect(model().attribute("visit", this.upcoming))
			.andExpect(view().name("pets/createOrUpdateVisitForm"));
	}

	@Test
	void processEditVisitFormUpdatesTheVisit() throws Exception {
		LocalDate newDate = LocalDate.now().plusDays(7);
		mockMvc
			.perform(post("/owners/{ownerId}/pets/{petId}/visits/{visitId}/edit", TEST_OWNER_ID, TEST_PET_ID,
					UPCOMING_VISIT_ID)
				.param("date", newDate.toString())
				.param("description", "rabies booster"))
			.andExpect(status().is3xxRedirection())
			.andExpect(view().name("redirect:/owners/{ownerId}"));
		assertThat(this.upcoming.getDate()).isEqualTo(newDate);
		assertThat(this.upcoming.getDescription()).isEqualTo("rabies booster");
		assertThat(this.pet.getVisits()).hasSize(2);
		verify(this.owners).save(any(Owner.class));
	}

	@Test
	void processEditVisitFormRejectsABlankDescription() throws Exception {
		mockMvc
			.perform(post("/owners/{ownerId}/pets/{petId}/visits/{visitId}/edit", TEST_OWNER_ID, TEST_PET_ID,
					UPCOMING_VISIT_ID)
				.param("date", LocalDate.now().plusDays(7).toString())
				.param("description", ""))
			.andExpect(model().attributeHasFieldErrors("visit", "description"))
			.andExpect(status().isOk())
			.andExpect(view().name("pets/createOrUpdateVisitForm"));
		assertThat(this.upcoming.getDescription()).isEqualTo("rabies shot");
		verify(this.owners, never()).save(any(Owner.class));
	}

	@Test
	void processEditVisitFormRejectsADateThatIsNotInTheFuture() throws Exception {
		mockMvc
			.perform(post("/owners/{ownerId}/pets/{petId}/visits/{visitId}/edit", TEST_OWNER_ID, TEST_PET_ID,
					UPCOMING_VISIT_ID)
				.param("date", LocalDate.now().toString())
				.param("description", "rabies booster"))
			.andExpect(model().attributeHasFieldErrorCode("visit", "date", "typeMismatch.visitDate"))
			.andExpect(status().isOk())
			.andExpect(view().name("pets/createOrUpdateVisitForm"));
		verify(this.owners, never()).save(any(Owner.class));
	}

	@Test
	void processCancelVisitRemovesTheVisit() throws Exception {
		mockMvc
			.perform(post("/owners/{ownerId}/pets/{petId}/visits/{visitId}/cancel", TEST_OWNER_ID, TEST_PET_ID,
					UPCOMING_VISIT_ID))
			.andExpect(status().is3xxRedirection())
			.andExpect(view().name("redirect:/owners/{ownerId}"));
		assertThat(this.pet.getVisits()).containsExactly(this.past);
		verify(this.owners).save(any(Owner.class));
	}

	@Test
	void pastVisitsCannotBeEditedOrCancelled() throws Exception {
		mockMvc
			.perform(get("/owners/{ownerId}/pets/{petId}/visits/{visitId}/edit", TEST_OWNER_ID, TEST_PET_ID,
					PAST_VISIT_ID))
			.andExpect(status().isBadRequest());
		mockMvc
			.perform(post("/owners/{ownerId}/pets/{petId}/visits/{visitId}/cancel", TEST_OWNER_ID, TEST_PET_ID,
					PAST_VISIT_ID))
			.andExpect(status().isBadRequest());
		assertThat(this.pet.getVisits()).hasSize(2);
		verify(this.owners, never()).save(any(Owner.class));
	}

	@Test
	void unknownVisitsAreNotFound() throws Exception {
		mockMvc.perform(post("/owners/{ownerId}/pets/{petId}/visits/{visitId}/cancel", TEST_OWNER_ID, TEST_PET_ID, 999))
			.andExpect(status().isNotFound());
		mockMvc
			.perform(get("/owners/{ownerId}/pets/{petId}/visits/{visitId}/edit", TEST_OWNER_ID, 999, UPCOMING_VISIT_ID))
			.andExpect(status().isNotFound());
		verify(this.owners, never()).save(any(Owner.class));
	}

}
