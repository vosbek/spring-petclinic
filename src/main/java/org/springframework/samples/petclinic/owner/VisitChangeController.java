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

import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Controller;
import org.springframework.ui.Model;
import org.springframework.validation.BindingResult;
import org.springframework.web.bind.WebDataBinder;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.InitBinder;
import org.springframework.web.bind.annotation.ModelAttribute;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.server.ResponseStatusException;
import org.springframework.web.servlet.mvc.support.RedirectAttributes;

import jakarta.validation.Valid;

/**
 * Changing and cancelling upcoming visits. Kept apart from {@link VisitController}, whose
 * model attribute adds a new visit to the pet before every request.
 */
@Controller
class VisitChangeController {

	private static final String VIEW_VISIT_FORM = "pets/createOrUpdateVisitForm";

	private final OwnerRepository owners;

	public VisitChangeController(OwnerRepository owners) {
		this.owners = owners;
	}

	@InitBinder
	public void setAllowedFields(WebDataBinder dataBinder) {
		dataBinder.setDisallowedFields("id", "*.id");
	}

	@ModelAttribute("minVisitDate")
	public LocalDate minVisitDate() {
		return LocalDate.now().plusDays(1);
	}

	@GetMapping("/owners/{ownerId}/pets/{petId}/visits/{visitId}/edit")
	public String initEditVisitForm(@PathVariable("ownerId") int ownerId, @PathVariable("petId") int petId,
			@PathVariable("visitId") int visitId, Model model) {
		Owner owner = findOwner(ownerId);
		Pet pet = findPet(owner, petId);
		Visit visit = findUpcomingVisit(pet, visitId);
		model.addAttribute("owner", owner);
		model.addAttribute("pet", pet);
		model.addAttribute("visit", visit);
		return VIEW_VISIT_FORM;
	}

	@PostMapping("/owners/{ownerId}/pets/{petId}/visits/{visitId}/edit")
	public String processEditVisitForm(@PathVariable("ownerId") int ownerId, @PathVariable("petId") int petId,
			@PathVariable("visitId") int visitId, @Valid @ModelAttribute("visit") Visit form, BindingResult result,
			Model model, RedirectAttributes redirectAttributes) {
		Owner owner = findOwner(ownerId);
		Pet pet = findPet(owner, petId);
		Visit visit = findUpcomingVisit(pet, visitId);
		if (result.hasErrors()) {
			form.setId(visit.getId());
			model.addAttribute("owner", owner);
			model.addAttribute("pet", pet);
			return VIEW_VISIT_FORM;
		}
		visit.setDate(form.getDate());
		visit.setDescription(form.getDescription());
		this.owners.save(owner);
		redirectAttributes.addFlashAttribute("message", "Your visit has been updated");
		return "redirect:/owners/{ownerId}";
	}

	@PostMapping("/owners/{ownerId}/pets/{petId}/visits/{visitId}/cancel")
	public String processCancelVisit(@PathVariable("ownerId") int ownerId, @PathVariable("petId") int petId,
			@PathVariable("visitId") int visitId, RedirectAttributes redirectAttributes) {
		Owner owner = findOwner(ownerId);
		Pet pet = findPet(owner, petId);
		Visit visit = findUpcomingVisit(pet, visitId);
		pet.removeVisit(visit);
		this.owners.save(owner);
		redirectAttributes.addFlashAttribute("message", "Your visit has been cancelled");
		return "redirect:/owners/{ownerId}";
	}

	private Owner findOwner(int ownerId) {
		return this.owners.findById(ownerId)
			.orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND, "Owner not found: " + ownerId));
	}

	private Pet findPet(Owner owner, int petId) {
		Pet pet = owner.getPet(petId);
		if (pet == null) {
			throw new ResponseStatusException(HttpStatus.NOT_FOUND, "Pet not found for this owner: " + petId);
		}
		return pet;
	}

	private Visit findUpcomingVisit(Pet pet, int visitId) {
		Visit visit = pet.getVisit(visitId);
		if (visit == null) {
			throw new ResponseStatusException(HttpStatus.NOT_FOUND, "Visit not found for this pet: " + visitId);
		}
		if (!visit.isUpcoming()) {
			throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Past visits cannot be changed: " + visitId);
		}
		return visit;
	}

}
